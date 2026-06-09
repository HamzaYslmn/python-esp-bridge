"""Generic H-bridge DC-motor driver over the bridge's PWM + GPIO primitives.

Covers the most popular hobby driver boards by their two common control
schemes (no I2C — just PWM channels and plain GPIO):

  Scheme 1 — PWM-on-enable + 2 direction pins
    L298N    : ENA = PWM (speed), IN1/IN2 = direction (HIGH/LOW = fwd,
               LOW/HIGH = rev, both equal = brake/coast).
    TB6612FNG: PWMA = PWM, AIN1/AIN2 = direction, plus optional STBY
               (drive HIGH to take the chip out of standby).
    See: https://zbotic.in/motor-driver-ic-comparison-l298n-vs-drv8833-vs-tb6612fng/
         https://www.pololu.com/product/713 (TB6612FNG truth table)

  Scheme 2 — two-PWM (no separate enable)
    DRV8833 (and TB6612 wired this way): IN1/IN2 are *both* PWM-capable.
    forward = PWM on IN1, IN2 low ; reverse = PWM on IN2, IN1 low ;
    both low = coast ; both high = brake. Magnitude is the PWM duty.
    See: https://newscrewdriver.com/2021/03/03/tb6612-vs-drv8833-dc-motor-driver-ics-for-esp32-micro-sawppy/

Usage:
    from espbridge.drivers.motor import Motor

    # L298N: ENA on 25, IN1/IN2 on 26/27
    m = Motor(esp, in1=26, in2=27, enable=25)
    m.forward(0.8)        # 80% duty forward
    m.set_speed(-0.5)     # 50% duty reverse
    m.stop()              # coast
    m.brake()            # active brake

    # DRV8833: two-PWM, IN1/IN2 on 18/19
    m2 = Motor(esp, in1=18, in2=19)
    m2.backward(1.0)
"""
from __future__ import annotations


class Motor:
    """A single DC motor on a generic H-bridge.

    Two control schemes, selected by whether ``enable`` is given:
      * ``enable`` set   -> scheme 1: ``enable`` is the PWM speed pin,
        ``in1``/``in2`` are plain GPIO direction pins.
      * ``enable`` None  -> scheme 2: ``in1``/``in2`` are both PWM pins.
    """

    def __init__(self, bridge, in1: int, in2: int, *, enable: int | None = None,
                 stby: int | None = None, freq: int = 1000):
        if freq <= 0:
            raise ValueError("freq must be positive")
        if in1 == in2:
            raise ValueError("in1 and in2 must be different pins")
        self._gpio = bridge.gpio
        self._pwm = bridge.pwm
        self._in1 = in1
        self._in2 = in2
        self._enable = enable
        self._stby = stby
        self._freq = freq

        if enable is not None:
            # Scheme 1: direction pins are plain outputs, speed via PWM on enable.
            self._gpio.mode(in1, "output")
            self._gpio.mode(in2, "output")
            self._pwm.attach(enable, freq=freq)
        else:
            # Scheme 2: both inputs are PWM channels.
            self._pwm.attach(in1, freq=freq)
            self._pwm.attach(in2, freq=freq)

        if stby is not None:
            # Take the chip out of standby (TB6612 STBY active-high = enabled).
            self._gpio.mode(stby, "output")
            self._gpio.write(stby, 1)

        self.stop()

    @staticmethod
    def _clamp(speed: float) -> float:
        """Clamp signed speed to [-1.0, 1.0]."""
        return max(-1.0, min(1.0, float(speed)))

    def set_speed(self, speed: float) -> None:
        """Drive at signed ``speed`` in [-1.0, 1.0]: sign = direction,
        magnitude = duty %. ``0`` coasts."""
        speed = self._clamp(speed)
        duty = abs(speed) * 100.0
        if self._enable is not None:
            # Scheme 1: set direction on the GPIO pins, duty on the enable PWM.
            if speed >= 0:
                self._gpio.write(self._in1, 1)
                self._gpio.write(self._in2, 0)
            else:
                self._gpio.write(self._in1, 0)
                self._gpio.write(self._in2, 1)
            self._pwm.duty_pct(self._enable, duty)
        else:
            # Scheme 2: PWM on the leading pin, the other held low (0% duty).
            if speed >= 0:
                self._pwm.duty_pct(self._in1, duty)
                self._pwm.duty_pct(self._in2, 0)
            else:
                self._pwm.duty_pct(self._in1, 0)
                self._pwm.duty_pct(self._in2, duty)

    def forward(self, speed: float = 1.0) -> None:
        """Spin forward at ``speed`` in [0, 1.0] (magnitude only)."""
        self.set_speed(abs(float(speed)))   # set_speed() clamps

    def backward(self, speed: float = 1.0) -> None:
        """Spin backward at ``speed`` in [0, 1.0] (magnitude only)."""
        self.set_speed(-abs(float(speed)))  # set_speed() clamps

    def stop(self) -> None:
        """Coast: cut drive (duty 0 / both inputs low). Motor free-wheels."""
        if self._enable is not None:
            self._pwm.duty_pct(self._enable, 0)
            self._gpio.write(self._in1, 0)
            self._gpio.write(self._in2, 0)
        else:
            self._pwm.duty_pct(self._in1, 0)
            self._pwm.duty_pct(self._in2, 0)

    def brake(self) -> None:
        """Active brake: short the motor terminals (both direction inputs HIGH)."""
        if self._enable is not None:
            # Both direction pins high; keep the chip enabled at full duty so
            # the short is actually applied across the windings.
            self._gpio.write(self._in1, 1)
            self._gpio.write(self._in2, 1)
            self._pwm.duty_pct(self._enable, 100)
        else:
            # Two-PWM: both inputs at 100% duty = both HIGH = brake.
            self._pwm.duty_pct(self._in1, 100)
            self._pwm.duty_pct(self._in2, 100)

    def deinit(self) -> None:
        """Stop the motor and release the PWM channel(s)."""
        self.stop()
        if self._enable is not None:
            self._pwm.detach(self._enable)
        else:
            self._pwm.detach(self._in1)
            self._pwm.detach(self._in2)
