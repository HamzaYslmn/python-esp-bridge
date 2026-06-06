"""TCP/UDP sockets proxied through the ESP32 radio.

Socket-like objects backed by the NET module's credit-window flow control:
the firmware never sends more than the window un-acked; we replenish credit
as the application consumes data, so fast peers get normal TCP backpressure.
"""
from __future__ import annotations

import queue
import struct
import threading
import time

from . import constants as C
from .errors import BridgeError, BridgeTimeoutError
from .protocol import ip_str, lp

# Per-request send chunk: bounds single-request size (and how long the
# firmware's synchronous socket write can run). Well under MAX_PAYLOAD.
_SEND_CHUNK = 1024


_UNSET = object()


class TcpSocket:
    def __init__(self, net: "Net", handle: int, peer: tuple[str, int] | None = None):
        self._net = net
        self._b = net._b
        self.handle = handle
        self.peer = peer
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._open = True
        self._timeout: float | None = None

    def settimeout(self, timeout: float | None) -> None:
        """Default timeout for recv() — stdlib socket semantics."""
        self._timeout = timeout

    def gettimeout(self) -> float | None:
        return self._timeout

    # internal: called from the reader thread
    def _feed(self, data: bytes) -> None:
        with self._cond:
            self._buf += data
            self._cond.notify_all()

    def _closed_remote(self) -> None:
        with self._cond:
            self._open = False
            self._cond.notify_all()

    @property
    def connected(self) -> bool:
        return self._open

    def recv(self, maxbytes: int = 65536, timeout: float | None = _UNSET) -> bytes:
        """Like socket.recv: b'' means the peer closed. Blocks up to `timeout`
        (default: the settimeout() value; None blocks until data/close)."""
        if timeout is _UNSET:
            timeout = self._timeout
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while not self._buf:
                if not self._open:
                    return b""
                wait = None if deadline is None else deadline - time.monotonic()
                if wait is not None and wait <= 0:
                    raise BridgeTimeoutError("recv timed out")
                self._cond.wait(wait if wait is not None else 0.5)
            n = min(maxbytes, len(self._buf))
            data = bytes(self._buf[:n])
            del self._buf[:n]
        # Replenish the firmware's send window (fire-and-forget).
        self._b.send(C.NET_WINDOW_ACK, struct.pack(">BH", self.handle, n))
        return data

    def recv_exactly(self, n: int, timeout: float | None = None) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = self.recv(n - len(out), timeout)
            if not chunk:
                raise BridgeError("connection closed mid-read")
            out += chunk
        return bytes(out)

    def send(self, data: bytes) -> int:
        """Send all of `data`; returns len(data)."""
        data = bytes(data)
        off = 0
        while off < len(data):
            chunk = data[off : off + _SEND_CHUNK]
            r = self._b.request(C.NET_SEND, bytes([self.handle]) + chunk, timeout=10.0)
            (sent,) = struct.unpack(">H", r)
            if sent == 0:
                raise BridgeError("send failed (socket closed?)")
            off += sent
        return len(data)

    sendall = send

    def close(self) -> None:
        if self._open:
            self._open = False
            try:
                self._b.request(C.NET_CLOSE, bytes([self.handle]))
            except BridgeError:
                pass
        self._net._forget(self.handle)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TcpServer:
    def __init__(self, net: "Net", handle: int, port: int):
        self._net = net
        self.handle = handle
        self.port = port
        self._accepted: queue.Queue[TcpSocket] = queue.Queue()

    def accept(self, timeout: float | None = None) -> TcpSocket:
        try:
            return self._accepted.get(timeout=timeout)
        except queue.Empty:
            raise BridgeTimeoutError("no incoming connection") from None

    def close(self) -> None:
        try:
            self._net._b.request(C.NET_CLOSE, bytes([self.handle]))
        except BridgeError:
            pass
        self._net._forget(self.handle)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class UdpSocket:
    def __init__(self, net: "Net", handle: int, local_port: int):
        self._net = net
        self._b = net._b
        self.handle = handle
        self.local_port = local_port
        self._timeout: float | None = None
        self._packets: queue.Queue[tuple[bytes, tuple[str, int]]] = queue.Queue(maxsize=256)

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def gettimeout(self) -> float | None:
        return self._timeout

    def _feed(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            self._packets.put_nowait((data, addr))
        except queue.Full:
            pass  # UDP: drop on overflow

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        ip = bytes(int(x) for x in addr[0].split("."))
        if len(data) > C.MAX_PAYLOAD - 7:
            raise ValueError(f"datagram too large (> {C.MAX_PAYLOAD - 7})")
        self._b.request(C.NET_SEND_TO,
                        bytes([self.handle]) + ip + struct.pack(">H", addr[1]) + bytes(data))

    def recvfrom(self, timeout: float | None = _UNSET) -> tuple[bytes, tuple[str, int]]:
        if timeout is _UNSET:
            timeout = self._timeout
        try:
            return self._packets.get(timeout=timeout)
        except queue.Empty:
            raise BridgeTimeoutError("no datagram received") from None

    def close(self) -> None:
        try:
            self._b.request(C.NET_CLOSE, bytes([self.handle]))
        except BridgeError:
            pass
        self._net._forget(self.handle)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Net:
    def __init__(self, bridge):
        self._b = bridge
        self._sockets: dict[int, object] = {}
        bridge.on_event(C.NET_DATA_EVT, self._on_data)
        bridge.on_event(C.NET_UDP_EVT, self._on_udp)
        bridge.on_event(C.NET_ACCEPT_EVT, self._on_accept)
        bridge.on_event(C.NET_CLOSED_EVT, self._on_closed)

    # ---- events (reader thread) ------------------------------------------------

    def _on_data(self, p: bytes) -> None:
        s = self._sockets.get(p[0]) if p else None
        if isinstance(s, TcpSocket):
            s._feed(p[1:])

    def _on_udp(self, p: bytes) -> None:
        if len(p) < 7:
            return
        s = self._sockets.get(p[0])
        if isinstance(s, UdpSocket):
            (port,) = struct.unpack_from(">H", p, 5)
            s._feed(p[7:], (ip_str(p[1:5]), port))

    def _on_accept(self, p: bytes) -> None:
        if len(p) < 8:
            return
        srv = self._sockets.get(p[0])
        if isinstance(srv, TcpServer):
            (port,) = struct.unpack_from(">H", p, 6)
            sock = TcpSocket(self, p[1], (ip_str(p[2:6]), port))
            self._sockets[p[1]] = sock
            srv._accepted.put(sock)

    def _on_closed(self, p: bytes) -> None:
        s = self._sockets.get(p[0]) if p else None
        if isinstance(s, TcpSocket):
            s._closed_remote()

    def _forget(self, handle: int) -> None:
        self._sockets.pop(handle, None)

    # ---- API -----------------------------------------------------------------------

    def tcp_connect(self, host: str, port: int, timeout: float = 10.0) -> TcpSocket:
        """Open a TCP connection *through the ESP32's Wi-Fi*."""
        r = self._b.request(C.NET_TCP_CONNECT,
                            struct.pack(">H", port) + lp(host),
                            timeout=timeout)
        sock = TcpSocket(self, r[0], (host, port))
        self._sockets[r[0]] = sock
        return sock

    def tcp_listen(self, port: int) -> TcpServer:
        r = self._b.request(C.NET_TCP_LISTEN, struct.pack(">H", port))
        srv = TcpServer(self, r[0], port)
        self._sockets[r[0]] = srv
        return srv

    def udp(self, local_port: int = 0) -> UdpSocket:
        r = self._b.request(C.NET_UDP_OPEN, struct.pack(">H", local_port))
        sock = UdpSocket(self, r[0], local_port)
        self._sockets[r[0]] = sock
        return sock

    def http_get(self, url: str, timeout: float = 15.0) -> tuple[int, bytes]:
        """Tiny HTTP/1.0 GET through the bridge (no TLS). Returns (status, body)."""
        if not url.startswith("http://"):
            raise ValueError("only http:// supported (no TLS on the proxy path)")
        rest = url[7:]
        host, _, path = rest.partition("/")
        host, _, port_s = host.partition(":")
        with self.tcp_connect(host, int(port_s) if port_s else 80, timeout) as s:
            s.send(f"GET /{path} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            raw = bytearray()
            while True:
                chunk = s.recv(timeout=timeout)
                if not chunk:
                    break
                raw += chunk
        head, _, body = bytes(raw).partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1]) if b" " in head else 0
        return status, body
