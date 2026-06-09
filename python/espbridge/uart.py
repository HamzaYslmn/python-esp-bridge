"""Secondary UARTs (1, 2): write from host, RX streamed back as events."""
from __future__ import annotations

import struct
import threading

from . import constants as C

# Maximum bytes sent in a single UART_WRITE request. Keeping this bounded
# limits how long the firmware's synchronous write can block on a slow baud rate.
_WRITE_CHUNK = 1024


class UartPort:
    """Bridged UART with a pyserial-like surface (read/write/in_waiting/...)."""

    def __init__(self, bridge, port: int):
        self._b = bridge
        self.port = port
        self.timeout: float | None = None  # default for read()/read_until()
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._callbacks: list = []

    def _feed(self, data: bytes) -> None:
        with self._cond:
            self._buf += data
            self._cond.notify_all()
        for cb in list(self._callbacks):
            cb(data)

    def write(self, data: bytes) -> None:
        """Send bytes out the port (chunked into <= 1 KB requests)."""
        data = bytes(data)
        for off in range(0, len(data), _WRITE_CHUNK):
            self._b.request(C.UART_WRITE, bytes([self.port]) + data[off : off + _WRITE_CHUNK])

    def read(self, n: int | None = None, timeout: float | None = None) -> bytes:
        """Read up to n buffered bytes (all if n is None); waits up to `timeout`
        (default: self.timeout, pyserial-style; None blocks until data)."""
        timeout = self.timeout if timeout is None else timeout
        with self._cond:
            if not self._buf and timeout != 0:
                self._cond.wait(timeout)
            n = len(self._buf) if n is None else min(n, len(self._buf))
            data = bytes(self._buf[:n])
            del self._buf[:n]
            return data

    @property
    def in_waiting(self) -> int:
        """Bytes already received and buffered (pyserial-compatible)."""
        with self._cond:
            return len(self._buf)

    def reset_input_buffer(self) -> None:
        """Discard all buffered received bytes (pyserial-compatible)."""
        with self._cond:
            self._buf.clear()

    def flush(self) -> None:
        """No-op (writes are synchronous requests); for pyserial compatibility."""
        pass  # writes are synchronous requests; nothing left to flush

    def readline(self, timeout: float | None = None) -> bytes:
        """Read until newline (or timeout); returns what arrived, partial included."""
        t = timeout if timeout is not None else (self.timeout or 2.0)
        return self.read_until(b"\n", t)

    def read_until(self, sep: bytes = b"\n", timeout: float = 2.0) -> bytes:
        """Read until `sep` is seen; on timeout returns whatever is buffered."""
        import time
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                i = self._buf.find(sep)
                if i >= 0:
                    data = bytes(self._buf[: i + len(sep)])
                    del self._buf[: i + len(sep)]
                    return data
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    data = bytes(self._buf)
                    self._buf.clear()
                    return data
                self._cond.wait(remaining)

    def on_rx(self, callback) -> None:
        """callback(bytes) on every RX chunk (runs on the reader thread)."""
        self._callbacks.append(callback)

    def close(self) -> None:
        """Tear down the port on the firmware (stops RX streaming)."""
        self._b.request(C.UART_DEINIT, bytes([self.port]))


class Uart:
    """Secondary UART manager; init() returns a pyserial-like UartPort.

        port = esp.uart.init(port=1, tx=17, rx=16, baud=9600)
        port.write(b"AT\\r\\n")
        print(port.readline())
    """

    def __init__(self, bridge):
        self._b = bridge
        self._ports: dict[int, UartPort] = {}
        bridge.on_event(C.UART_RX_EVT, self._on_rx)

    def _on_rx(self, payload: bytes) -> None:
        if len(payload) < 2:
            return
        port = self._ports.get(payload[0])
        if port is not None:
            port._feed(payload[1:])

    def init(self, *, port: int = 1, tx: int = 17, rx: int = 16,
             baud: int = 115_200) -> UartPort:
        """Open/configure a secondary UART and return its UartPort."""
        self._b.request(C.UART_INIT, struct.pack(">BbbI", port, tx, rx, baud))
        p = self._ports.get(port)
        if p is None:
            p = UartPort(self._b, port)
            self._ports[port] = p
        return p
