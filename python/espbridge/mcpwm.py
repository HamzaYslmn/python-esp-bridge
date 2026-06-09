"""MCPWM: complementary PWM pair with hardware deadtime — for H-bridge /
half-bridge drivers where both switches must never conduct at once. Plain
PWM lives in esp.pwm (LEDC). Not on ESP32-S2/C3 (no MCPWM peripheral).

    esp.mcpwm.begin(pin_a=25, pin_b=26, freq=20_000, deadtime_ns=500)
    esp.mcpwm.duty(35.0)   # percent on pin_a; pin_b is the inverse
    esp.mcpwm.stop()
"""
from __future__ import annotations

import struct

from . import constants as C


class Mcpwm:
    """Complementary PWM pair with hardware deadtime (reached via ``esp.mcpwm``).

        esp = espbridge.connect()
        esp.mcpwm.begin(pin_a=25, pin_b=26, freq=20_000, deadtime_ns=500)
        esp.mcpwm.duty(35.0)   # percent on pin_a; pin_b is the inverse
        esp.mcpwm.stop()
    """

    def __init__(self, bridge):
        self._b = bridge
        bridge.require(C.Cap.MCPWM, "MCPWM")

    def begin(self, pin_a: int, pin_b: int | None = None, *,
              freq: int = 20_000, deadtime_ns: int = 500) -> None:
        """Start the PWM generator at `freq` Hz. If pin_b is None, only
        pin_a is driven (no complementary output, no deadtime insertion).
        deadtime_ns is resolved in 100 ns steps; maximum value is 6553 ns."""
        self._b.request(C.MCPWM_INIT,
                        struct.pack(">BbIH", pin_a,
                                    -1 if pin_b is None else pin_b,
                                    freq, deadtime_ns))

    def duty(self, percent: float) -> None:
        """0..100% on pin_a (pin_b runs the complement minus deadtime)."""
        pm = round(max(0.0, min(100.0, percent)) * 10)  # firmware expects tenths of a percent (0..1000)
        self._b.request(C.MCPWM_DUTY, struct.pack(">H", pm))

    def stop(self) -> None:
        """Stop the generator and release both pins."""
        self._b.request(C.MCPWM_STOP)

    end = stop  # Arduino-style alias (begin() already exists)
