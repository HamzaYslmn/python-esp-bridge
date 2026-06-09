"""PWM via the ESP32 LEDC peripheral, plus servo/tone conveniences."""
from __future__ import annotations

import struct

from . import constants as C


def _full_scale(res_bits: int) -> int:
    """Largest raw LEDC duty count for a given resolution (2**res_bits - 1)."""
    return (1 << res_bits) - 1


class Pwm:
    """PWM output via the LEDC peripheral, plus servo/tone helpers.

        esp.pwm.attach(2, freq=1000)
        esp.pwm.duty_pct(2, 50)        # 50% duty
        esp.pwm.detach(2)
    """

    def __init__(self, bridge):
        self._b = bridge
        self._attached: dict[int, tuple[int, int]] = {}  # pin -> (freq_hz, resolution_bits)

    def attach(self, pin: int, freq: int = 1000, resolution_bits: int = 10) -> None:
        """Start a PWM channel on a pin at `freq` Hz with the given duty resolution."""
        self._b.request(C.PWM_ATTACH, struct.pack(">BIB", pin, freq, resolution_bits))
        self._attached[pin] = (freq, resolution_bits)

    def write(self, pin: int, duty: int) -> None:
        """Raw duty (0 .. 2**resolution_bits - 1)."""
        self._b.request(C.PWM_WRITE, struct.pack(">BI", pin, duty))

    def duty_pct(self, pin: int, percent: float) -> None:
        """Set duty cycle as a percentage (0..100); auto-attaches the pin if needed."""
        if pin not in self._attached:
            self.attach(pin)
        _, res = self._attached[pin]
        self.write(pin, round(_full_scale(res) * max(0.0, min(100.0, percent)) / 100.0))

    def detach(self, pin: int) -> None:
        """Stop PWM on a pin and free its LEDC channel."""
        self._b.request(C.PWM_DETACH, bytes([pin]))
        self._attached.pop(pin, None)

    def tone(self, pin: int, freq: int) -> None:
        """Square wave at `freq` Hz (0 = off) — buzzer-friendly."""
        self._b.request(C.PWM_TONE, struct.pack(">BI", pin, freq))

    def servo(self, pin: int, angle: float, *, min_us: int = 500, max_us: int = 2500) -> None:
        """Drive a hobby servo: 50 Hz, angle 0..180 mapped to min_us..max_us."""
        if self._attached.get(pin) != (50, 14):  # servo needs 50 Hz, 14-bit resolution
            self.attach(pin, 50, 14)
        us = min_us + (max_us - min_us) * max(0.0, min(180.0, angle)) / 180.0
        self.write(pin, round(us / 20000.0 * _full_scale(14)))
