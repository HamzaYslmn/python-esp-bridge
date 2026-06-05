"""I2C master (buses 0 and 1)."""
from __future__ import annotations

import struct

from . import constants as C


class I2c:
    def __init__(self, bridge):
        self._b = bridge

    def init(self, *, sda: int = 21, scl: int = 22, freq: int = 400_000, bus: int = 0) -> None:
        self._b.request(C.I2C_INIT, struct.pack(">BBBI", bus, sda, scl, freq))

    def scan(self, bus: int = 0) -> list[int]:
        """Addresses (7-bit) that ACK on the bus."""
        r = self._b.request(C.I2C_SCAN, bytes([bus]), timeout=5.0)
        return list(r[1 : 1 + r[0]])

    def write(self, addr: int, data: bytes, bus: int = 0) -> None:
        if len(data) > C.MAX_PAYLOAD - 2:  # frame carries bus + addr first
            raise ValueError(f"max {C.MAX_PAYLOAD - 2} bytes per I2C write")
        self._b.request(C.I2C_WRITE, bytes([bus, addr]) + bytes(data))

    def read(self, addr: int, n: int, bus: int = 0) -> bytes:
        if not 1 <= n <= 255:
            raise ValueError("read length must be 1..255")
        return self._b.request(C.I2C_READ, bytes([bus, addr, n]))

    def write_read(self, addr: int, wdata: bytes, rlen: int, bus: int = 0) -> bytes:
        """Write then read with a repeated start (typical register read)."""
        if len(wdata) > 255 or not 1 <= rlen <= 255:
            raise ValueError("wdata max 255 bytes, rlen 1..255")
        payload = bytes([bus, addr, len(wdata)]) + bytes(wdata) + bytes([rlen])
        return self._b.request(C.I2C_WRITE_READ, payload)

    def read_reg(self, addr: int, reg: int, n: int = 1, bus: int = 0) -> bytes:
        return self.write_read(addr, bytes([reg]), n, bus)

    def write_reg(self, addr: int, reg: int, data: bytes | int, bus: int = 0) -> None:
        data = bytes([data]) if isinstance(data, int) else bytes(data)
        self.write(addr, bytes([reg]) + data, bus)

    def deinit(self, bus: int = 0) -> None:
        self._b.request(C.I2C_DEINIT, bytes([bus]))
