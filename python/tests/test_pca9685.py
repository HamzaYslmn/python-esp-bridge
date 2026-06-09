"""espbridge.drivers.pca9685.PCA9685: 16-channel 12-bit PWM / servo driver."""
import pytest

from espbridge.drivers.pca9685 import PCA9685

MODE1 = 0x00
PRESCALE = 0xFE
LED0_ON_L = 0x06


class FakeI2c:
    """Records register/raw writes; serves a per-register memory for reads."""

    def __init__(self):
        self.inited = None
        self.calls = []          # ("write_reg"|"write", ...)
        self.regs = {MODE1: 0x11}  # power-on MODE1 = SLEEP|ALLCALL (datasheet)

    def init(self, *, sda=21, scl=22, freq=400_000, bus=0):
        self.inited = (sda, scl, bus)

    def write_reg(self, addr, reg, data, bus=0):
        val = data if isinstance(data, int) else bytes(data)
        if isinstance(val, int):
            self.regs[reg] = val
        self.calls.append(("write_reg", addr, reg, val, bus))

    def read_reg(self, addr, reg, n=1, bus=0):
        return bytes([self.regs.get(reg, 0)] * n)

    def write(self, addr, data, bus=0):
        self.calls.append(("write", addr, bytes(data), bus))


class FakeEsp:
    def __init__(self):
        self.i2c = FakeI2c()


def test_init_does_sleep_prescale_dance_for_50hz():
    esp = FakeEsp()
    PCA9685(esp, address=0x40, freq=50)
    calls = esp.i2c.calls

    # AI enabled first.
    assert calls[0] == ("write_reg", 0x40, MODE1, 0x20, 0)

    # The set_freq sequence: sleep -> prescale -> wake -> restart.
    writes = [c for c in calls if c[0] == "write_reg"]
    regs_written = [(c[2], c[3]) for c in writes]
    # SLEEP bit (0x10) set before PRESCALE write.
    sleep_call = next(c for c in writes if c[2] == MODE1 and c[3] & 0x10)
    prescale_call = next(c for c in writes if c[2] == PRESCALE)
    assert calls.index(sleep_call) < calls.index(prescale_call)

    # prescale = round(25e6 / (4096*50)) - 1 = 121.
    assert prescale_call[3] == 121

    # A RESTART (0x80) write lands after the prescale write.
    restart_call = next(c for c in writes if c[2] == MODE1 and c[3] & 0x80)
    assert calls.index(prescale_call) < calls.index(restart_call)


def test_set_servo_90deg_writes_led0_bytes_for_1500us():
    esp = FakeEsp()
    pwm = PCA9685(esp, freq=50)
    esp.i2c.calls.clear()
    pwm.set_servo(0, 90)               # 1500 us at 50 Hz
    # count = round(1500 * 50 * 4096 / 1e6) = 307 ; ON=0, OFF=307 (0x0133).
    assert esp.i2c.calls == [
        ("write", 0x40, bytes([LED0_ON_L, 0x00, 0x00, 0x33, 0x01]), 0),
    ]


def test_set_pwm_byte_order():
    esp = FakeEsp()
    pwm = PCA9685(esp, freq=50)
    esp.i2c.calls.clear()
    pwm.set_pwm(2, 0x111, 0x222)       # ON=0x111, OFF=0x222
    reg = LED0_ON_L + 4 * 2
    # ON_L, ON_H, OFF_L, OFF_H.
    assert esp.i2c.calls == [
        ("write", 0x40, bytes([reg, 0x11, 0x01, 0x22, 0x02]), 0),
    ]


def test_duty_quarter():
    esp = FakeEsp()
    pwm = PCA9685(esp, freq=1000)
    esp.i2c.calls.clear()
    pwm.duty(3, 0.25)                  # off = round(0.25*4096) = 1024 (0x0400)
    reg = LED0_ON_L + 4 * 3
    assert esp.i2c.calls == [
        ("write", 0x40, bytes([reg, 0x00, 0x00, 0x00, 0x04]), 0),
    ]


def test_off_sets_full_off_bit():
    esp = FakeEsp()
    pwm = PCA9685(esp, freq=50)
    esp.i2c.calls.clear()
    pwm.off(5)
    reg = LED0_ON_L + 4 * 5
    # OFF_H bit4 (0x10) = full-off.
    assert esp.i2c.calls == [
        ("write", 0x40, bytes([reg, 0x00, 0x00, 0x00, 0x10]), 0),
    ]


def test_init_with_pins_initialises_bus():
    esp = FakeEsp()
    PCA9685(esp, sda=4, scl=5, bus=1)
    assert esp.i2c.inited == (4, 5, 1)


def test_bad_address_channel_and_args_rejected():
    esp = FakeEsp()
    with pytest.raises(ValueError):
        PCA9685(esp, address=0x20)
    pwm = PCA9685(esp)
    with pytest.raises(ValueError):
        pwm.set_pwm(16, 0, 0)
    with pytest.raises(ValueError):
        pwm.set_pwm(0, 0, 5000)
    with pytest.raises(ValueError):
        pwm.duty(0, 1.5)
    with pytest.raises(ValueError):
        pwm.set_servo(0, 200)
