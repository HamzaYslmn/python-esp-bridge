"""Step/dir stepper driver (A4988/DRV8825/TMC-style) over the RMT primitive.

Pulse timing is generated on the ESP32; ramps and position live in Python.

    from espbridge.stepper import Stepper
    motor = Stepper(esp, step_pin=12, dir_pin=14)
    motor.move(400, speed=800, accel=1600)   # blocks until done
    motor.run(-200); ...; motor.stop()       # continuous
"""
from __future__ import annotations

import math

from .rmt import Symbol

_PULSE = 10        # step-pulse high time, us (drivers need >=1-2 us)
_MAX_SYM = 0x7FFF  # RMT symbol duration limit
_CHUNK = 1000      # symbols per RMT_TX request (payload-bounded)


def _step_syms(period_us: int) -> list[Symbol]:
    """One step: short high pulse + low for the rest (split to fit u15)."""
    syms: list[Symbol] = [(1, _PULSE)]
    low = max(period_us - _PULSE, 1)
    while low > _MAX_SYM:
        syms.append((0, _MAX_SYM))
        low -= _MAX_SYM
    syms.append((0, low))
    return syms


def ramp_periods(steps: int, speed: float, accel: float) -> list[int]:
    """Pure planner: per-step periods (us) for a trapezoidal/triangular move."""
    if accel <= 0:
        return [round(1e6 / speed)] * steps
    out = []
    for i in range(steps):
        v_acc = math.sqrt(2 * accel * (i + 1))
        v_dec = math.sqrt(2 * accel * (steps - i))
        out.append(round(1e6 / min(speed, v_acc, v_dec)))
    return out


class Stepper:
    def __init__(self, bridge, step_pin: int, dir_pin: int | None = None, *,
                 invert_dir: bool = False):
        self._rmt = bridge.rmt
        self._gpio = bridge.gpio
        self._step = step_pin
        self._dir = dir_pin
        self._invert = invert_dir
        self.position = 0
        self._rmt.init_tx(step_pin, 1_000_000)
        if dir_pin is not None:
            self._gpio.mode(dir_pin, "output")

    def _set_dir(self, forward: bool) -> None:
        if self._dir is not None:
            self._gpio.write(self._dir, int(forward ^ self._invert))

    def move(self, steps: int, speed: float = 500, accel: float = 0) -> None:
        """Move `steps` (sign = direction) at `speed` steps/s; blocks.

        accel > 0 ramps up/down (steps/s^2). Long moves are sent in chunks;
        a chunk boundary may pause the motor for one link round-trip (~ms),
        which steppers tolerate (no steps are lost, motion just hiccups).
        """
        if steps == 0:
            return
        self._set_dir(steps > 0)
        syms: list[Symbol] = []
        for period in ramp_periods(abs(steps), speed, accel):
            syms += _step_syms(period)
            if len(syms) >= _CHUNK:
                self._rmt.tx(self._step, syms)
                syms = []
        if syms:
            self._rmt.tx(self._step, syms)
        self.position += steps

    def run(self, speed: float) -> None:
        """Spin continuously at `speed` steps/s (sign = direction) until stop().

        Position is NOT tracked while free-running.
        """
        self._set_dir(speed > 0)
        self._rmt.tx_loop(self._step, _step_syms(round(1e6 / abs(speed))))

    def stop(self) -> None:
        self._rmt.tx_stop(self._step)

    def deinit(self) -> None:
        self._rmt.deinit(self._step)
