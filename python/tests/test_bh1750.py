"""espbridge.drivers.bh1750.BH1750: light sensor command bytes + lux decode."""
import pytest

from espbridge.drivers.bh1750 import (
    BH1750, POWER_ON, CONT_HIRES, CONT_HIRES2, CONT_LORES,
)


class FakeI2c:
    def __init__(self, raw=0x0000):
        self.inited = None
        self.writes = []          # (addr, bytes, bus)
        self._raw = raw

    def init(self, *, sda=21, scl=22, freq=400_000, bus=0):
        self.inited = (sda, scl, bus)

    def write(self, addr, data, bus=0):
        self.writes.append((addr, bytes(data), bus))

    def read(self, addr, n, bus=0):
        return bytes([(self._raw >> 8) & 0xFF, self._raw & 0xFF])[:n]


class FakeEsp:
    def __init__(self, raw=0x0000):
        self.i2c = FakeI2c(raw)


def test_init_sends_power_on_then_mode():
    esp = FakeEsp()
    BH1750(esp, address=0x23)
    assert esp.i2c.inited is None             # bus untouched when no pins given
    assert esp.i2c.writes == [
        (0x23, bytes([POWER_ON]), 0),
        (0x23, bytes([CONT_HIRES]), 0),       # default mode
    ]


def test_init_with_explicit_mode():
    esp = FakeEsp()
    BH1750(esp, mode=CONT_LORES)
    assert esp.i2c.writes[1] == (0x23, bytes([CONT_LORES]), 0)


def test_read_lux_hires_decode():
    esp = FakeEsp(raw=0x0CFC)                  # 3324 counts
    light = BH1750(esp)
    assert light.read_lux() == pytest.approx(3324 / 1.2)   # == 2770.0


def test_read_lux_hires2_halves():
    esp = FakeEsp(raw=0x0CFC)
    light = BH1750(esp, mode=CONT_HIRES2)
    assert light.read_lux() == pytest.approx(3324 / 1.2 / 2)


def test_init_with_pins_initialises_bus():
    esp = FakeEsp()
    BH1750(esp, address=0x5C, sda=4, scl=5, bus=1)
    assert esp.i2c.inited == (4, 5, 1)


def test_bad_address_rejected():
    esp = FakeEsp()
    with pytest.raises(ValueError):
        BH1750(esp, address=0x24)
