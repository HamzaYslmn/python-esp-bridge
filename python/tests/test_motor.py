"""espbridge.drivers.motor.Motor: generic H-bridge DC-motor driver."""
import pytest

from espbridge.drivers.motor import Motor


class FakeGpio:
    def __init__(self):
        self.modes = {}          # pin -> mode
        self.writes = []         # ordered (pin, value)

    def mode(self, pin, mode):
        self.modes[pin] = mode

    def write(self, pin, value):
        self.writes.append((pin, int(value)))

    def last(self, pin):
        """Most recent value written to `pin`, or None."""
        for p, v in reversed(self.writes):
            if p == pin:
                return v
        return None


class FakePwm:
    def __init__(self):
        self.attached = {}       # pin -> freq
        self.duties = []         # ordered (pin, pct)

    def attach(self, pin, freq=1000, resolution_bits=10):
        self.attached[pin] = freq

    def duty_pct(self, pin, percent):
        self.duties.append((pin, percent))

    def write(self, pin, duty):
        self.duties.append((pin, duty))

    def detach(self, pin):
        self.attached.pop(pin, None)

    def last(self, pin):
        for p, v in reversed(self.duties):
            if p == pin:
                return v
        return None


class FakeEsp:
    def __init__(self):
        self.gpio = FakeGpio()
        self.pwm = FakePwm()


# ---------------------------------------------------------------- scheme 1

def test_scheme1_setup():
    esp = FakeEsp()
    Motor(esp, in1=26, in2=27, enable=25)
    assert esp.gpio.modes == {26: "output", 27: "output"}
    assert 25 in esp.pwm.attached       # enable is the PWM pin
    assert 26 not in esp.pwm.attached    # direction pins stay plain GPIO


def test_scheme1_forward():
    esp = FakeEsp()
    m = Motor(esp, in1=26, in2=27, enable=25)
    m.forward(0.8)
    assert esp.gpio.last(26) == 1
    assert esp.gpio.last(27) == 0
    assert esp.pwm.last(25) == pytest.approx(80.0)


def test_scheme1_backward_flips_direction():
    esp = FakeEsp()
    m = Motor(esp, in1=26, in2=27, enable=25)
    m.backward(0.5)
    assert esp.gpio.last(26) == 0
    assert esp.gpio.last(27) == 1
    assert esp.pwm.last(25) == pytest.approx(50.0)


def test_scheme1_stop_zero_duty():
    esp = FakeEsp()
    m = Motor(esp, in1=26, in2=27, enable=25)
    m.forward(1.0)
    m.stop()
    assert esp.pwm.last(25) == pytest.approx(0.0)
    assert esp.gpio.last(26) == 0
    assert esp.gpio.last(27) == 0


def test_scheme1_brake_both_high():
    esp = FakeEsp()
    m = Motor(esp, in1=26, in2=27, enable=25)
    m.brake()
    assert esp.gpio.last(26) == 1
    assert esp.gpio.last(27) == 1


def test_stby_driven_high():
    esp = FakeEsp()
    Motor(esp, in1=26, in2=27, enable=25, stby=14)
    assert esp.gpio.modes[14] == "output"
    assert esp.gpio.last(14) == 1


# ---------------------------------------------------------------- scheme 2

def test_scheme2_setup():
    esp = FakeEsp()
    Motor(esp, in1=18, in2=19)
    assert 18 in esp.pwm.attached and 19 in esp.pwm.attached
    assert esp.gpio.modes == {}          # no plain direction pins


def test_scheme2_forward_pwm_on_in1_in2_low():
    esp = FakeEsp()
    m = Motor(esp, in1=18, in2=19)
    m.forward(0.7)
    assert esp.pwm.last(18) == pytest.approx(70.0)
    assert esp.pwm.last(19) == pytest.approx(0.0)


def test_scheme2_backward_pwm_on_in2_in1_low():
    esp = FakeEsp()
    m = Motor(esp, in1=18, in2=19)
    m.backward(0.6)
    assert esp.pwm.last(18) == pytest.approx(0.0)
    assert esp.pwm.last(19) == pytest.approx(60.0)


def test_scheme2_brake_both_full_duty():
    esp = FakeEsp()
    m = Motor(esp, in1=18, in2=19)
    m.brake()
    assert esp.pwm.last(18) == pytest.approx(100.0)
    assert esp.pwm.last(19) == pytest.approx(100.0)


# ---------------------------------------------------------------- common

def test_speed_clamping():
    esp = FakeEsp()
    m = Motor(esp, in1=26, in2=27, enable=25)
    m.set_speed(5.0)                     # clamps to 1.0 -> 100% forward
    assert esp.gpio.last(26) == 1
    assert esp.pwm.last(25) == pytest.approx(100.0)
    m.set_speed(-5.0)                    # clamps to -1.0 -> 100% reverse
    assert esp.gpio.last(27) == 1
    assert esp.pwm.last(25) == pytest.approx(100.0)


def test_invalid_args_rejected():
    esp = FakeEsp()
    with pytest.raises(ValueError):
        Motor(esp, in1=5, in2=5, enable=25)   # same pins
    with pytest.raises(ValueError):
        Motor(esp, in1=5, in2=6, freq=0)      # bad freq
