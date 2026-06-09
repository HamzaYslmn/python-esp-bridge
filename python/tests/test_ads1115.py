"""espbridge.drivers.ads1115.ADS1115: 16-bit ADC single-shot config + decode."""
import struct

import pytest

from espbridge.drivers.ads1115 import ADS1115


class FakeI2c:
    def __init__(self, conversion=0x0000):
        self.inited = None
        self.writes = []          # (addr, bytes, bus)
        self.reads = []           # (addr, n, bus)
        self._conversion = conversion

    def init(self, *, sda=21, scl=22, freq=400_000, bus=0):
        self.inited = (sda, scl, bus)

    def write(self, addr, data, bus=0):
        self.writes.append((addr, bytes(data), bus))

    def write_reg(self, addr, reg, data, bus=0):
        data = bytes([data]) if isinstance(data, int) else bytes(data)
        self.write(addr, bytes([reg]) + data, bus)

    def read(self, addr, n, bus=0):
        self.reads.append((addr, n, bus))
        return struct.pack(">h", self._conversion)[:n]

    def read_reg(self, addr, reg, n=1, bus=0):
        self.write(addr, bytes([reg]), bus)   # repeated-start: reg pointer write
        return self.read(addr, n, bus)


class FakeEsp:
    def __init__(self, conversion=0x0000):
        self.i2c = FakeI2c(conversion)


def test_config_word_for_channel_and_gain():
    esp = FakeEsp()
    adc = ADS1115(esp, address=0x48, gain=4.096, data_rate=128)
    assert esp.i2c.inited is None             # bus untouched when no pins given
    adc.read(0)
    # First write is the Config register (0x01) start: OS=1, MUX=A0(0b100),
    # PGA=4.096(0b001), MODE=1, DR=128(0b100), COMP_QUE=11.
    expected_cfg = (1 << 15) | (0b100 << 12) | (0b001 << 9) | (1 << 8) | (0b100 << 5) | 0b11
    assert expected_cfg == 0xC383
    addr, data, bus = esp.i2c.writes[0]
    assert addr == 0x48 and bus == 0
    assert data == bytes([0x01]) + struct.pack(">H", expected_cfg)


def test_config_word_channel3():
    esp = FakeEsp()
    adc = ADS1115(esp)                         # gain=2.048, dr=128 defaults
    adc.read(3)
    expected_cfg = (1 << 15) | (0b111 << 12) | (0b010 << 9) | (1 << 8) | (0b100 << 5) | 0b11
    _, data, _ = esp.i2c.writes[0]
    assert data == bytes([0x01]) + struct.pack(">H", expected_cfg)


def test_read_decodes_signed_raw():
    esp = FakeEsp(conversion=0x7FFF)          # max positive code
    adc = ADS1115(esp, gain=2.048)
    assert adc.read(0) == 0x7FFF
    # full-scale: 32767/32768 * 2.048 V
    assert adc.read_voltage(0) == pytest.approx(0x7FFF / 32768 * 2.048)


def test_read_decodes_negative_raw():
    esp = FakeEsp(conversion=-32768)          # most-negative code
    adc = ADS1115(esp, gain=4.096)
    assert adc.read(1) == -32768
    assert adc.read_voltage(1) == pytest.approx(-4.096)


def test_init_with_pins_initialises_bus():
    esp = FakeEsp()
    ADS1115(esp, address=0x49, sda=4, scl=5, bus=1)
    assert esp.i2c.inited == (4, 5, 1)


def test_bad_args_rejected():
    esp = FakeEsp()
    with pytest.raises(ValueError):
        ADS1115(esp, address=0x47)
    with pytest.raises(ValueError):
        ADS1115(esp, gain=3.3)
    with pytest.raises(ValueError):
        ADS1115(esp, data_rate=100)
    adc = ADS1115(esp)
    with pytest.raises(ValueError):
        adc.read(4)
