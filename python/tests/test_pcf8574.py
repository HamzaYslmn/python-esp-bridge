"""espbridge.drivers.pcf8574.PCF8574: I2C GPIO expander read/modify/write."""
import pytest

from espbridge.drivers.pcf8574 import PCF8574


class FakeI2c:
    def __init__(self, in_byte=0xFF):
        self.inited = None
        self.writes = []
        self._in = in_byte

    def init(self, *, sda=21, scl=22, freq=400_000, bus=0):
        self.inited = (sda, scl, bus)

    def write(self, addr, data, bus=0):
        self.writes.append((addr, bytes(data), bus))

    def read(self, addr, n, bus=0):
        return bytes([self._in]) * n


class FakeEsp:
    def __init__(self, in_byte=0xFF):
        self.i2c = FakeI2c(in_byte)


def test_write_port_sends_one_byte():
    esp = FakeEsp()
    io = PCF8574(esp, address=0x20)
    assert esp.i2c.inited is None          # bus untouched when no pins given
    io.write_port(0b1010_0101)
    assert esp.i2c.writes == [(0x20, bytes([0xA5]), 0)]


def test_init_with_pins_initialises_bus():
    esp = FakeEsp()
    PCF8574(esp, address=0x21, sda=4, scl=5, bus=1)
    assert esp.i2c.inited == (4, 5, 1)


def test_write_pin_is_read_modify_write():
    esp = FakeEsp()
    io = PCF8574(esp)                       # power-on latch state = 0xFF
    io.write_pin(0, 0)                      # clear P0
    io.write_pin(7, 0)                      # clear P7
    assert [w[1] for w in esp.i2c.writes] == [bytes([0xFE]), bytes([0x7E])]


def test_read_port_and_pin():
    esp = FakeEsp(in_byte=0b0000_0100)      # only P2 high
    io = PCF8574(esp)
    assert io.read_port() == 0b0000_0100
    assert io.read_pin(2) == 1
    assert io.read_pin(3) == 0


def test_bad_address_and_pin_rejected():
    esp = FakeEsp()
    with pytest.raises(ValueError):
        PCF8574(esp, address=0x10)
    io = PCF8574(esp)
    with pytest.raises(ValueError):
        io.write_pin(8, 1)
