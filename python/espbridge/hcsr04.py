"""HC-SR04 ultrasonic distance sensor over the RMT primitive.

    from espbridge.hcsr04 import HCSR04
    sonar = HCSR04(esp, trig=5, echo=18)
    print(sonar.distance_cm())

Note: HC-SR04 is a 5 V part — level-shift (or voltage-divide) ECHO down to
3.3 V before wiring it to the ESP32.
"""
from __future__ import annotations


def echo_to_cm(symbols: list[tuple[int, int]]) -> float | None:
    """Pure decoder: first high pulse (1 us ticks) -> centimeters."""
    for level, dur in symbols:
        if level == 1 and dur >= 60:  # 60 us ~ 1 cm; rejects short crosstalk spikes from the trigger pulse
            return dur / 58.0
    return None


class HCSR04:
    def __init__(self, bridge, trig: int, echo: int):
        self._rmt = bridge.rmt
        self._trig = trig
        self._echo = echo
        self._rmt.init_rx(echo, 1_000_000)

    def distance_cm(self) -> float | None:
        """One ping -> distance in cm, or None when out of range (>~4 m)."""
        # Echo pulse can be up to ~25 ms for a valid return (38 ms means no
        # object detected). The idle threshold must be larger than the longest
        # possible echo so the RMT capture waits for the full pulse to finish.
        syms = self._rmt.recv(self._echo, idle_ticks=30_000, timeout_ms=100,
                              max_symbols=16, trigger=(self._trig, 1, 10))
        return echo_to_cm(syms)

    def deinit(self) -> None:
        self._rmt.deinit(self._echo)
