"""Hub — share one board across many processes over a local socket.

A USB/BLE link to a board can only be opened once, so two processes can't each
``espbridge.connect()`` to the same board: the second steals or corrupts the
first. The hub fixes that the way the firmware already multiplexes USB and BLE —
**one owner, the same COBS frame stream relayed over another link**. Here the
extra link is a localhost TCP socket:

    # owner process — holds the real BLE link, exposes it to others
    import espbridge
    espbridge.hub.serve(ble="c0:49:ef:d0:3f:e0").serve_forever()

    # any other process — attaches over the socket, drives the board normally
    esp = espbridge.connect(share="127.0.0.1:8787")
    esp.gpio.write(2, 1)

The owner runs a self-healing :class:`~espbridge.manager.BridgeManager` (with
``keepalive``), so a board reset/drop transparently reconnects under all the
clients. Each client request is relayed through the owner Bridge's own
``request()``, so the board's single seq-space is never double-used — client seq
numbers stay local to each client's Bridge. Board events are fanned out to every
client.
"""
from __future__ import annotations

import socketserver
import threading

from . import constants as C
from ._log import log
from .errors import BridgeError, BridgeTimeoutError, RemoteError
from .manager import shared_manager
from .protocol import FrameSplitter, decode_frame, encode_frame
from .transports.socket import DEFAULT_PORT, parse_addr


class _Client:
    """One connected client socket; its send() is serialized (reply + event fan-out
    both write here from different threads)."""

    def __init__(self, sock):
        self._sock = sock
        self._lock = threading.Lock()

    def send(self, data: bytes) -> None:
        with self._lock:
            try:
                self._sock.sendall(data)
            except OSError:
                pass  # client went away; the handler loop will clean it up


class _Hub:
    """Relays frames between many clients and the one owner Bridge."""

    def __init__(self, manager):
        self._mgr = manager
        self._cur = None                       # current owner Bridge (changes on reconnect)
        self._info = None                      # cached SYS_INFO payload for the handshake
        self._clients: set[_Client] = set()
        self._clients_lock = threading.Lock()

    # ---- owner link ----------------------------------------------------------
    def owner(self):
        """The live owner Bridge, (re)connecting via the manager. Re-registers the
        event fan-out whenever the underlying Bridge changes (i.e. after a
        reconnect — a fresh Bridge starts with no handlers)."""
        b = self._mgr.bridge()
        if b is not self._cur:
            self._cur = b
            b.on_event(None, self._on_board_event)  # forward every board event to clients
            try:
                self._info = b.request(C.SYS_INFO)
            except BridgeError:
                pass  # keep the last good info; a client connecting now still handshakes
        return b

    def info_payload(self) -> bytes | None:
        if self._info is None:
            try:
                self.owner()
            except BridgeError:
                pass
        return self._info

    # ---- clients -------------------------------------------------------------
    def add_client(self, c: _Client) -> None:
        with self._clients_lock:
            self._clients.add(c)

    def remove_client(self, c: _Client) -> None:
        with self._clients_lock:
            self._clients.discard(c)

    def _on_board_event(self, frame) -> None:
        data = encode_frame(frame.flags, frame.seq, frame.cmd, frame.payload)
        with self._clients_lock:
            clients = list(self._clients)
        for c in clients:
            c.send(data)

    # ---- relay ---------------------------------------------------------------
    def relay(self, frame, client: _Client) -> None:
        """Run one client frame against the owner Bridge and reply to that client.

        Called in order on the client's handler thread, so a client's own frames
        stay sequenced (the fire-and-forget writes before a waited write keep
        their order — e.g. an OLED frame burst then its final sync)."""
        cmd, seq, payload = frame.cmd, frame.seq, frame.payload
        if frame.is_event:
            return  # clients don't originate events
        if cmd == C.SYS_AUTH:
            client.send(encode_frame(0, seq, cmd))  # local socket is trusted: ack without the board
            return
        try:
            owner = self.owner()
        except BridgeError:
            if seq:
                client.send(encode_frame(C.FLAG_ERROR, seq, cmd, bytes([int(C.Status.IO)])))
            return
        if seq == 0:                              # fire-and-forget: no reply expected
            try:
                owner.send(cmd, payload)
            except BridgeError:
                pass
            return
        try:
            resp = owner.request(cmd, payload)
            client.send(encode_frame(0, seq, cmd, resp))
        except RemoteError as e:
            client.send(encode_frame(C.FLAG_ERROR, seq, cmd, bytes([int(e.status)])))
        except BridgeTimeoutError:
            pass  # let the client's own wait expire (mirrors a dropped reply on a real link)
        except BridgeError:
            pass


class _ClientHandler(socketserver.BaseRequestHandler):
    def handle(self):
        hub: _Hub = self.server.hub
        client = _Client(self.request)
        hub.add_client(client)
        try:
            info = hub.info_payload()  # synthesize the handshake: SYS_READY + board info
            if info is not None:
                client.send(encode_frame(C.FLAG_EVENT, 0, C.SYS_READY, info))
            splitter = FrameSplitter()
            while True:
                data = self.request.recv(4096)
                if not data:
                    break
                for chunk in splitter.feed(data):
                    try:
                        frame = decode_frame(chunk)
                    except BridgeError:
                        continue  # corrupt frame: the client retries on timeout
                    hub.relay(frame, client)
        except OSError:
            pass
        finally:
            hub.remove_client(client)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True       # don't let client threads block process exit
    allow_reuse_address = True  # rebind immediately after a restart (TIME_WAIT)


class Hub:
    """A running hub: owns the board link, serves clients. Use :func:`serve`."""

    def __init__(self, server: _Server, manager):
        self._server = server
        self.manager = manager

    @property
    def address(self) -> tuple[str, int]:
        """The (host, port) the hub is bound to (port is resolved if 0 was given)."""
        return self._server.server_address

    def serve_forever(self) -> None:
        """Serve clients until :meth:`stop` (blocking)."""
        try:
            self._server.serve_forever()
        finally:
            self._server.server_close()

    def start(self) -> "Hub":
        """Serve in a background daemon thread; returns self."""
        threading.Thread(target=self.serve_forever, name="espbridge-hub",
                         daemon=True).start()
        return self

    def stop(self) -> None:
        self._server.shutdown()


def serve(share: str | None = None, *, keepalive: float | None = 20.0,
          **connect_kwargs) -> Hub:
    """Own one board link and relay it to local clients over a socket.

    ``share`` is the ``host:port`` to bind (default ``127.0.0.1:8787``); pass
    ``:0`` to let the OS pick a free port (read it back via ``Hub.address``).
    ``connect_kwargs`` are forwarded to the Bridge (``ble=``, ``mac=``, ``name=``,
    ``port=``, ``password=`` ...). The owner runs with a ``keepalive`` heartbeat so
    a dropped board link reconnects under the clients.

    Returns a :class:`Hub`; call ``.serve_forever()`` (blocking) or ``.start()``
    (background thread).
    """
    host, port = parse_addr(share) if share else ("127.0.0.1", DEFAULT_PORT)
    # Bind FIRST: if the port is taken (another owner is already running), raise
    # before touching the board — so a lost election never steals the live link.
    server = _Server((host, port), _ClientHandler)
    manager = shared_manager(keepalive=keepalive, **connect_kwargs)
    hub = _Hub(manager)
    server.hub = hub
    try:
        hub.owner()  # open the board link now (raises if no board), register fan-out
    except BaseException:
        server.server_close()
        raise
    log.info(f"espbridge hub: sharing {manager.connect_kwargs or 'auto'} "
             f"on {server.server_address[0]}:{server.server_address[1]}")
    return Hub(server, manager)
