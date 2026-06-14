"""Socket transport — share one board across processes via a local hub.

A board's USB/BLE link can only be opened once, so several processes can't each
``connect()`` to the same board. The hub (see :mod:`espbridge.hub`) solves this:
ONE owner process holds the real Bridge and relays the exact same COBS frame
stream to many clients over a localhost TCP socket. This transport is the client
end — the same blocking ``read()``/``write()`` contract as the serial and BLE
transports, carrying frames over the socket instead of a wire.

    esp = espbridge.connect(share="127.0.0.1:8787")   # attach to a running hub
"""
from __future__ import annotations

import socket

from ..errors import NoDeviceError

DEFAULT_PORT = 8787


def parse_addr(addr: str) -> tuple[str, int]:
    """'host:port' / ':port' / 'host' -> (host, port), filling defaults."""
    host, _, port = addr.partition(":")
    return host or "127.0.0.1", int(port) if port else DEFAULT_PORT


class SocketTransport:
    """Frame transport over a TCP socket to an espbridge hub (see espbridge.hub)."""

    has_baud = False    # a socket has no wire baud to negotiate
    needs_auth = False  # a local hub socket is trusted (like USB); the hub authed the board
    usb_chip = None

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, *,
                 connect_timeout: float = 5.0):
        self.host, self.port = host, port
        try:
            self._sock = socket.create_connection((host, port), timeout=connect_timeout)
        except OSError as e:
            raise NoDeviceError(
                f"no espbridge hub at {host}:{port} — start one with the owner "
                f"process (espbridge.hub.serve / `espbridge hub`)"
            ) from e
        self._sock.settimeout(None)  # blocking reads; close() unblocks via shutdown()
        # TCP_NODELAY: frames are latency-sensitive and small, so don't let Nagle
        # hold a frame back waiting to coalesce with the next one.
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._closed = False

    def read(self) -> bytes:
        # Blocks until bytes arrive or the peer closes (recv -> b""), which the
        # Bridge reader loop treats as "link down".
        try:
            return self._sock.recv(4096)
        except OSError:
            return b""

    def write(self, data: bytes) -> None:
        self._sock.sendall(data)

    def set_baudrate(self, baud: int) -> None:
        pass  # socket: no baud

    def pulse_reset(self) -> None:
        pass  # a shared client must not reset the board out from under the others

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)  # wake a read() blocked in recv()
        except OSError:
            pass
        self._sock.close()
