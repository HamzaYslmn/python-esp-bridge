"""DS18B20 / DS18S20 temperature sensors over the 1-Wire bus.

    from espbridge.ds18b20 import DS18B20
    probe = DS18B20(esp, pin=4)            # first sensor on the bus
    print(probe.read())                    # degrees C

    for rom in DS18B20.discover(esp, 4):   # several probes on one pin
        print(rom, DS18B20(esp, 4, rom).read())
"""
from __future__ import annotations

import time

from .onewire import crc8

_FAMILIES = (0x28, 0x10, 0x22)  # DS18B20, DS18S20, DS1822
CONVERT_T = 0x44
READ_SCRATCHPAD = 0xBE
WRITE_SCRATCHPAD = 0x4E

# 9..12-bit resolution -> worst-case conversion time (ms)
_CONV_MS = {9: 94, 10: 188, 11: 375, 12: 750}


def parse_scratchpad(data: bytes, family: int) -> float:
    """Pure decoder: 9 scratchpad bytes -> degrees C (raises on bad CRC)."""
    if len(data) != 9 or crc8(data) != 0:
        raise ValueError("scratchpad CRC mismatch")
    raw = int.from_bytes(data[:2], "little", signed=True)
    return raw / 2.0 if family == 0x10 else raw / 16.0


class DS18B20:
    def __init__(self, bridge, pin: int, rom: str | None = None):
        self._ow = bridge.onewire
        self._pin = pin
        if rom is None:
            found = self.discover(bridge, pin)
            if not found:
                raise OSError("no DS18x20 found on the bus")
            rom = found[0]
        self.rom = rom
        self._family = bytes.fromhex(rom)[0]
        self._resolution = 12

    @staticmethod
    def discover(bridge, pin: int) -> list[str]:
        return [r for r in bridge.onewire.search(pin)
                if bytes.fromhex(r)[0] in _FAMILIES]

    def set_resolution(self, bits: int) -> None:
        """9..12 bits (DS18B20/DS1822); fewer bits = faster conversions."""
        if bits not in _CONV_MS:
            raise ValueError("resolution must be 9..12")
        cfg = (bits - 9) << 5 | 0x1F
        self._ow.select(self._pin, self.rom)
        self._ow.write(self._pin, bytes([WRITE_SCRATCHPAD, 0, 0, cfg]))
        self._resolution = bits

    def read(self) -> float:
        """Start a conversion, wait, return degrees C."""
        self._ow.select(self._pin, self.rom)
        # power=True keeps parasite-powered sensors fed during conversion
        self._ow.write(self._pin, bytes([CONVERT_T]), power=True)
        time.sleep(_CONV_MS[self._resolution] / 1000)
        self._ow.select(self._pin, self.rom)
        self._ow.write(self._pin, bytes([READ_SCRATCHPAD]))
        return parse_scratchpad(self._ow.read(self._pin, 9), self._family)
