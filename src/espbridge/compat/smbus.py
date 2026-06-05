"""smbus2-compatible SMBus, so classic Raspberry Pi I2C code runs unchanged.

    from espbridge import Bridge
    from espbridge.compat.smbus import SMBus

    with Bridge() as esp:
        bus = SMBus(esp)                       # instead of smbus2.SMBus(1)
        temp = bus.read_byte_data(0x48, 0x00)  # the rest of the code is identical
        bus.write_i2c_block_data(0x20, 0x06, [0xFF, 0x00])

Word data is little-endian, matching the SMBus specification (and smbus2).
"""
from __future__ import annotations


class SMBus:
    def __init__(self, esp, *, sda: int = 21, scl: int = 22,
                 freq: int = 100_000, bus: int = 0, init: bool = True):
        self._i2c = esp.i2c
        self._bus = bus
        if init:
            self._i2c.init(sda=sda, scl=scl, freq=freq, bus=bus)

    # ---- plain byte ----------------------------------------------------------
    def read_byte(self, addr: int) -> int:
        return self._i2c.read(addr, 1, self._bus)[0]

    def write_byte(self, addr: int, value: int) -> None:
        self._i2c.write(addr, bytes([value & 0xFF]), self._bus)

    def write_quick(self, addr: int) -> None:
        self._i2c.write(addr, b"", self._bus)

    # ---- register (command) based -----------------------------------------------
    def read_byte_data(self, addr: int, register: int) -> int:
        return self._i2c.write_read(addr, bytes([register]), 1, self._bus)[0]

    def write_byte_data(self, addr: int, register: int, value: int) -> None:
        self._i2c.write(addr, bytes([register, value & 0xFF]), self._bus)

    def read_word_data(self, addr: int, register: int) -> int:
        lo, hi = self._i2c.write_read(addr, bytes([register]), 2, self._bus)
        return lo | (hi << 8)

    def write_word_data(self, addr: int, register: int, value: int) -> None:
        self._i2c.write(addr, bytes([register, value & 0xFF, (value >> 8) & 0xFF]),
                        self._bus)

    def read_i2c_block_data(self, addr: int, register: int, length: int = 32) -> list[int]:
        return list(self._i2c.write_read(addr, bytes([register]), length, self._bus))

    def write_i2c_block_data(self, addr: int, register: int, data) -> None:
        self._i2c.write(addr, bytes([register]) + bytes(data), self._bus)

    # ---- lifecycle -------------------------------------------------------------
    def close(self) -> None:
        pass  # the Bridge owns the link

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
