"""Drop-in-ish RPi.GPIO shim backed by the ESP32 bridge.

    from espbridge.compat import rpi_gpio as GPIO

    GPIO.setmode(GPIO.BCM)        # pin numbers are ESP32 GPIO numbers
    GPIO.setup(2, GPIO.OUT)
    GPIO.output(2, GPIO.HIGH)
    GPIO.add_event_detect(4, GPIO.FALLING, callback=lambda ch: print(ch))

Uses a process-wide Bridge (auto-detected port) unless you call attach(bridge).
"""
from __future__ import annotations

from ..bridge import Bridge

BCM = "BCM"
BOARD = "BOARD"  # accepted but identical: numbering is always ESP32 GPIO
OUT = "out"
IN = "in"
HIGH = 1
LOW = 0
PUD_UP = "up"
PUD_DOWN = "down"
PUD_OFF = "off"
RISING = "rising"
FALLING = "falling"
BOTH = "change"

_bridge: Bridge | None = None
_owned = False


def attach(bridge: Bridge) -> None:
    """Use an existing Bridge instead of auto-connecting."""
    global _bridge, _owned
    _bridge = bridge
    _owned = False


def _b() -> Bridge:
    global _bridge, _owned
    if _bridge is None:
        _bridge = Bridge()
        _owned = True
    return _bridge


def setmode(mode) -> None:  # numbering is always ESP32 GPIO numbers
    pass


def setwarnings(flag: bool) -> None:
    pass


def setup(channel, direction, pull_up_down=PUD_OFF, initial=None) -> None:
    channels = channel if isinstance(channel, (list, tuple)) else [channel]
    for ch in channels:
        if direction == OUT:
            _b().gpio.mode(ch, "output")
            if initial is not None:
                _b().gpio.write(ch, initial)
        else:
            mode = {PUD_UP: "input_pullup", PUD_DOWN: "input_pulldown"}.get(
                pull_up_down, "input")
            _b().gpio.mode(ch, mode)


def output(channel, value) -> None:
    channels = channel if isinstance(channel, (list, tuple)) else [channel]
    values = value if isinstance(value, (list, tuple)) else [value] * len(channels)
    if len(channels) > 1:
        _b().gpio.write_many(dict(zip(channels, values)))
    else:
        _b().gpio.write(channels[0], values[0])


def input(channel) -> int:  # noqa: A001 — mirrors RPi.GPIO's name
    return _b().gpio.read(channel)


def add_event_detect(channel, edge, callback=None, bouncetime=0) -> None:
    cb = (lambda ev, _cb=callback: _cb(ev.pin)) if callback else None
    _b().gpio.watch(channel, edge, debounce_ms=bouncetime, callback=cb)


def remove_event_detect(channel) -> None:
    _b().gpio.unwatch(channel)


class PWM:
    """RPi.GPIO-style PWM object."""

    def __init__(self, channel: int, frequency: float):
        self.channel = channel
        self.frequency = int(frequency)
        self._duty = 0.0

    def start(self, duty_cycle: float) -> None:
        _b().pwm.attach(self.channel, self.frequency, 12)
        self.ChangeDutyCycle(duty_cycle)

    def ChangeDutyCycle(self, duty_cycle: float) -> None:
        self._duty = duty_cycle
        _b().pwm.duty_pct(self.channel, duty_cycle)

    def ChangeFrequency(self, frequency: float) -> None:
        self.frequency = int(frequency)
        _b().pwm.attach(self.channel, self.frequency, 12)
        _b().pwm.duty_pct(self.channel, self._duty)

    def stop(self) -> None:
        _b().pwm.detach(self.channel)


def cleanup(channel=None) -> None:
    global _bridge, _owned
    if _bridge is not None and _owned:
        _bridge.close()
        _bridge = None
        _owned = False
