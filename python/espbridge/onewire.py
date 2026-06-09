"""1-Wire bus. The firmware provides only bit timing; ROM search, CRC8 and
addressing live here.

    devices = esp.onewire.search(pin=4)   # ['28b1642f050000c7', ...]
"""
from __future__ import annotations

from . import constants as C

SEARCH_ROM = 0xF0
ALARM_SEARCH = 0xEC
MATCH_ROM = 0x55
SKIP_ROM = 0xCC


def crc8(data: bytes) -> int:
    """Maxim/Dallas CRC8 (poly 0x31 reflected). crc8(rom_with_crc) == 0."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


class OneWire:
    """1-Wire bus master (reset/read/write + ROM search & addressing).

        roms = esp.onewire.search(pin=4)   # ['28b1642f050000c7', ...]
        esp.onewire.select(4, roms[0])
        esp.onewire.write(4, b"\\x44", power=True)  # DS18B20 convert-T
        data = esp.onewire.read(4, 9)               # scratchpad
    """

    def __init__(self, bridge):
        self._b = bridge
        bridge.require(C.Cap.ONEWIRE, "1-Wire")

    def reset(self, pin: int) -> bool:
        """Bus reset -> True if at least one device answered presence."""
        return self._b.request(C.OW_RESET, bytes([pin]))[0] == 1

    def write(self, pin: int, data: bytes, *, power: bool = False) -> None:
        """Write bytes; power=True leaves the line strongly driven high
        afterwards (parasite-powered temperature conversions)."""
        self._b.request(C.OW_WRITE, bytes([pin, 1 if power else 0]) + data)

    def read(self, pin: int, n: int) -> bytes:
        """Read n bytes from the bus (reads time slots, returns the bits)."""
        return self._b.request(C.OW_READ, bytes([pin, n]))

    def select(self, pin: int, rom: str | None) -> None:
        """Reset + address one device (or all when rom is None)."""
        if not self.reset(pin):
            raise OSError("no 1-Wire presence pulse (check wiring/pull-up)")
        if rom is None:
            self.write(pin, bytes([SKIP_ROM]))
        else:
            self.write(pin, bytes([MATCH_ROM]) + bytes.fromhex(rom))

    def _triplet(self, pin: int, direction: int) -> tuple[int, int, int]:
        # ROM search triplet: firmware reads two bits then writes one.
        # Returns (id_bit, complement_bit, direction_taken).
        r = self._b.request(C.OW_TRIPLET, bytes([pin, direction]))
        return r[0], r[1], r[2]

    def search(self, pin: int, *, alarm: bool = False) -> list[str]:
        """Enumerate device ROMs (hex, family code first) — Maxim algorithm."""
        roms: list[str] = []
        rom = bytearray(8)
        last_disc = 0
        while True:
            if not self.reset(pin):
                return roms
            self.write(pin, bytes([ALARM_SEARCH if alarm else SEARCH_ROM]))
            disc = 0
            for bit in range(64):
                byte_i, bit_i = divmod(bit, 8)
                if bit + 1 < last_disc:
                    hint = rom[byte_i] >> bit_i & 1
                else:
                    hint = 1 if bit + 1 == last_disc else 0
                id_bit, cmp_bit, taken = self._triplet(pin, hint)
                if id_bit and cmp_bit:
                    return roms  # both bits 1 means no devices remain on this branch
                if not id_bit and not cmp_bit and taken == 0:
                    disc = bit + 1  # remember this as the last fork where we chose 0
                if taken:
                    rom[byte_i] |= 1 << bit_i
                else:
                    rom[byte_i] &= ~(1 << bit_i)
            if crc8(bytes(rom)) == 0:
                roms.append(rom.hex())
            last_disc = disc
            if last_disc == 0:
                return roms
