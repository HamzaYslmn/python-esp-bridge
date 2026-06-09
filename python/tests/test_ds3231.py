"""espbridge.drivers.ds3231.DS3231: BCD time encode/decode + temperature."""
import pytest

from espbridge.drivers.ds3231 import DS3231


class FakeI2c:
    def __init__(self, regs=None):
        self.inited = None
        self.writes = []                 # (addr, bytes, bus) via write()
        self._regs = regs or {}          # reg -> bytes returned by read_reg

    def init(self, *, sda=21, scl=22, freq=400_000, bus=0):
        self.inited = (sda, scl, bus)

    def write(self, addr, data, bus=0):
        self.writes.append((addr, bytes(data), bus))

    def read_reg(self, addr, reg, n=1, bus=0):
        return self._regs[reg][:n]


class FakeEsp:
    def __init__(self, regs=None):
        self.i2c = FakeI2c(regs)


def test_set_encodes_to_bcd():
    esp = FakeEsp()
    rtc = DS3231(esp)
    # Tue 2026-06-09 14:30:45, weekday=2.
    rtc.set(2026, 6, 9, 14, 30, 45, weekday=2)
    addr, data, bus = esp.i2c.writes[0]
    assert addr == 0x68 and bus == 0
    # reg pointer 0x00 then BCD: sec min hour wday day month year.
    assert data == bytes([0x00, 0x45, 0x30, 0x14, 0x02, 0x09, 0x06, 0x26])


def test_now_decodes_canned_bcd():
    # Same instant as above, in the 7-byte time block at reg 0x00.
    regs = {0x00: bytes([0x45, 0x30, 0x14, 0x02, 0x09, 0x06, 0x26])}
    rtc = DS3231(FakeEsp(regs))
    assert rtc.now() == (2026, 6, 9, 14, 30, 45, 2)


def test_now_masks_control_bits():
    # hour reg has 12/24 bit6 set, month reg has century bit7 set: both ignored.
    regs = {0x00: bytes([0x00, 0x00, 0x40 | 0x23, 0x01, 0x01, 0x80 | 0x12, 0x99])}
    rtc = DS3231(FakeEsp(regs))
    year, month, day, hour, *_ = rtc.now()
    assert (year, month, day, hour) == (2099, 12, 1, 23)


def test_temperature_decodes_quarter_degrees():
    # 0x11 = 0x1C (28 °C), 0x12 = 0x40 (bits[7:6]=01 -> +0.25) => 28.25 °C.
    regs = {0x11: bytes([0x1C, 0x40])}
    assert DS3231(FakeEsp(regs)).temperature() == pytest.approx(28.25)


def test_temperature_negative():
    # 0x11 = 0xEC = -20 (signed int8), 0x12 = 0xC0 (+0.75) => -19.25 °C.
    regs = {0x11: bytes([0xEC, 0xC0])}
    assert DS3231(FakeEsp(regs)).temperature() == pytest.approx(-19.25)


def test_init_with_pins_initialises_bus():
    esp = FakeEsp()
    DS3231(esp, sda=4, scl=5, bus=1)
    assert esp.i2c.inited == (4, 5, 1)


def test_bad_args_rejected():
    with pytest.raises(ValueError):
        DS3231(FakeEsp(), address=0x57)
    rtc = DS3231(FakeEsp())
    with pytest.raises(ValueError):
        rtc.set(2026, 13, 1, 0, 0, 0)
    with pytest.raises(ValueError):
        rtc.set(2026, 1, 1, 24, 0, 0)
