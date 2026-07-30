"""Wi-Fi (TCP) transport — the bridge protocol over a socket instead of a cable.

All three links carry the identical COBS frame stream, so this is a thin socket
wrapper with the same blocking read()/write() interface as the serial one::

    esp = Bridge(host="192.168.1.50")   # board in listen mode
    esp = Bridge(wifi=True)             # find it with a UDP broadcast

The socket can also arrive already connected — that is how a board that dialed
home becomes an ordinary Bridge (see Bridge.all(wifi=True)).
"""
from __future__ import annotations

import contextlib
import select
import socket
import struct
import time
from dataclasses import dataclass

from .. import constants as C
from ..errors import BridgeError, NoDeviceError
from ..protocol import mac_to_str

DISCOVERY_PROBE = b"ESPB?"
DISCOVERY_REPLY = b"ESPB!"


class TcpTransport:
    """Socket transport with the same interface as SerialTransport."""

    has_baud = False    # no line rate to negotiate
    needs_auth = True   # a network link is not physical access: SYS_AUTH first
    usb_chip = None
    # Same firmware RX path as the serial link, so the same caps: three max-size
    # frames in flight saturates it without outrunning a slow handler.
    burst_window = 6400
    max_inflight = 6400

    def __init__(self, host: str | None = None, port: int = C.BRIDGE_LINK_PORT,
                 *, connect_timeout: float = 5.0, sock: socket.socket | None = None):
        """Dial ``host:port``, or adopt an already-connected ``sock``.

        Adopting is how a dial-home board becomes a Bridge: the accepted socket
        is handed straight in, so nothing about the board differs from one we
        dialed out to ourselves.
        """
        if sock is None:
            try:
                sock = socket.create_connection((host, port), connect_timeout)
            except OSError as e:
                raise NoDeviceError(
                    f"TCP connect to {host}:{port} failed: {e}") from None
        else:
            host, port = sock.getpeername()[:2]
        self.host, self.port, self.sock = host, port, sock
        # Without TCP_NODELAY, Nagle holds a small request back waiting for more
        # data and turns a 2 ms round trip into a 42 ms one.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Blocking with no timeout: the reader thread then sleeps in the kernel
        # instead of waking 20x a second to poll. That is what makes a thread per
        # board affordable at fleet scale — close() calls shutdown(), which is
        # what wakes this recv() up.
        sock.settimeout(None)

    def read(self) -> bytes:
        try:
            data = self.sock.recv(4096)
        except TimeoutError:
            return b""
        if not data:
            # Raise on a clean close: returning b"" would spin Bridge's reader
            # loop instead of declaring the link down.
            raise BridgeError(f"{self.host}:{self.port} closed the connection")
        return data

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def set_baudrate(self, baud: int) -> None:
        pass  # networked link

    def pulse_reset(self) -> None:
        pass  # no DTR/RTS over a socket

    def close(self) -> None:
        with contextlib.suppress(OSError):   # already-dead socket
            self.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.sock.close()


class TcpListener:
    """Server socket that hands each dial-home board back as a TcpTransport.

    Boards configured with ``wifi.begin(ssid, pass, "host:port")`` connect *out*
    to here and reconnect forever with jittered backoff, so nothing on this side
    tracks IP addresses and a restart is a non-event.
    """

    def __init__(self, port: int = C.BRIDGE_LINK_PORT, host: str = "0.0.0.0",
                 *, backlog: int = 512):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # No SO_REUSEADDR: on Windows it lets a SECOND process bind this port and
        # silently steal connections, which turns a forgotten server into an hour
        # of debugging.
        try:
            self.sock.bind((host, port))
        except OSError as e:
            self.sock.close()
            raise BridgeError(f"cannot listen on {host}:{port}: {e}") from None
        self.sock.listen(backlog)   # deep: a thousand boards return at once
        self.port = self.sock.getsockname()[1]

    def accept(self, timeout: float | None = 0.5) -> TcpTransport | None:
        """Next board to dial in, or None if `timeout` passed with nobody there.

        Also returns None once close() has run — settimeout/accept on a closed
        socket raise, and the caller's loop uses None as its cue to re-check.
        """
        try:
            self.sock.settimeout(timeout)
            peer, _ = self.sock.accept()
        except (TimeoutError, OSError):
            return None
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return TcpTransport(sock=peer)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()


@dataclass(frozen=True)
class WifiDeviceInfo:
    """A bridge that answered the discovery broadcast (cf. PortInfo/BleDeviceInfo)."""

    host: str
    port: int
    mac: str
    name: str = ""

    @property
    def ident(self) -> str:
        """What to pass to ``Bridge()`` for this board: its name, or its MAC."""
        return self.name or self.mac


def find_wifi_devices(timeout: float = 1.0,
                      port: int = C.BRIDGE_LINK_PORT) -> list[WifiDeviceInfo]:
    """Broadcast a UDP probe and collect every bridge that answers.

    Only *listen*-mode boards answer; a dial-home board announces itself by
    connecting instead. Broadcast does not cross subnets (or most Wi-Fi
    client-isolation settings), so pass ``host=`` when in doubt.
    """
    found: dict[str, WifiDeviceInfo] = {}
    socks = []
    try:
        # One socket bound per local address: a single unbound socket broadcasts
        # out whichever interface the routing table prefers, which on a machine
        # with Hyper-V/WSL/VPN adapters is regularly the wrong one.
        for local in _local_ipv4s():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((local, 0))
                s.setblocking(False)
                s.sendto(DISCOVERY_PROBE, ("255.255.255.255", port))
                socks.append(s)
            except OSError:
                continue
        if not socks:
            raise BridgeError("no usable network interface for discovery")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select(socks, [], [], 0.1)
            for s in ready:
                try:
                    data, addr = s.recvfrom(64)
                except OSError:
                    continue
                # "ESPB!" | mac[6] | port u16 | name[] — the name is the rest of
                # the datagram, so it needs no length byte.
                if not data.startswith(DISCOVERY_REPLY) or len(data) < 13:
                    continue
                mac = mac_to_str(data[5:11])
                (rport,) = struct.unpack_from(">H", data, 11)
                found[mac] = WifiDeviceInfo(addr[0], rport, mac,
                                            data[13:].decode("utf-8", "replace"))
    finally:
        for s in socks:
            s.close()
    return sorted(found.values(), key=lambda d: d.host)


def _local_ipv4s() -> list[str]:
    """Every IPv4 address this host owns, loopback last (it can't reach boards)."""
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    addrs.discard("127.0.0.1")
    return sorted(addrs) or ["0.0.0.0"]
