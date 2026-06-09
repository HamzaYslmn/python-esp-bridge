"""gpiozero pin factory — LED, Button, PWMLED & friends on ESP32 pins.

    pip install gpiozero

    from gpiozero import LED, Button
    from espbridge import Bridge
    from espbridge.compat.gpiozero import EspBridgeFactory

    esp = Bridge()
    factory = EspBridgeFactory(esp)

    led = LED(2, pin_factory=factory)          # ESP32 GPIO numbers
    btn = Button(4, pin_factory=factory)
    btn.when_pressed = led.toggle

    # or make it the default for every gpiozero device:
    from gpiozero import Device
    Device.pin_factory = factory

Supported: input/output, pull-up/down, edge detection with debounce
(``when_pressed``/``when_changed``), and PWM (``PWMLED``, ``value=0.5``).
"""
from __future__ import annotations

import time

from gpiozero.exc import PinInvalidFunction, PinInvalidPin, PinInvalidPull, PinSetInput
from gpiozero.pins import BoardInfo, Factory, HeaderInfo, Pin, PinInfo

_PWM_RES_BITS = 12
_PWM_MAX = (1 << _PWM_RES_BITS) - 1
_EDGE_MAP = {"both": "change", "rising": "rising", "falling": "falling"}
_PULL_MODES = {"floating": "input", "up": "input_pullup", "down": "input_pulldown"}


def _board_info(gpio_count: int, chip: str) -> BoardInfo:
    pins = {
        n: PinInfo(number=n, name=f"GPIO{n}",
                   names=frozenset({n, str(n), f"GPIO{n}"}),
                   pull="", row=n + 1, col=1, interfaces=frozenset({"gpio"}))
        for n in range(gpio_count)
    }
    header = HeaderInfo(name="GPIO", rows=gpio_count, columns=1, pins=pins)
    return BoardInfo(
        revision="espbridge", model=chip, pcb_revision="1.0", released="2026",
        soc=chip, manufacturer="Espressif", memory=0, storage="flash",
        usb=1, usb3=0, ethernet=0, eth_speed=0, wifi=True, bluetooth=True,
        csi=0, dsi=0, headers={"GPIO": header}, board="",
    )


class EspBridgePin(Pin):
    def __init__(self, factory: "EspBridgeFactory", info: PinInfo):
        super().__init__()
        self._factory = factory
        self._info = info
        self._gpio = factory._esp.gpio
        self._pwm = factory._esp.pwm
        self._n = info.number

        self._function = "input"
        self._pull = "floating"
        self._frequency: float | None = None
        self._duty = 0.0
        self._bounce: float | None = None
        self._edges = "both"
        self._when_changed = None
        self._watching = False
        self._gpio.mode(self._n, "input")

    def __repr__(self):
        return f"<EspBridgePin GPIO{self._n}>"

    # ---- identity -------------------------------------------------------------
    def _get_info(self):
        return self._info

    # ---- function -------------------------------------------------------------
    def _get_function(self):
        return self._function

    def _set_function(self, value):
        if value not in ("input", "output"):
            raise PinInvalidFunction(f"invalid function {value!r} for {self!r}")
        self._function = value
        if value == "input":
            self._gpio.mode(self._n, _PULL_MODES[self._pull])
        else:
            self._gpio.mode(self._n, "output")

    def output_with_state(self, state):
        self._function = "output"
        self._gpio.mode(self._n, "output")
        self._set_state(state)

    def input_with_pull(self, pull):
        self._pull = pull
        self._set_function("input")

    # ---- state ------------------------------------------------------------------
    def _get_state(self):
        if self._frequency is not None:
            return self._duty
        return self._gpio.read(self._n)

    def _set_state(self, value):
        if self._frequency is not None:
            self._duty = max(0.0, min(1.0, float(value)))
            self._pwm.write(self._n, round(self._duty * _PWM_MAX))
        elif self._function == "input":
            raise PinSetInput(f"cannot set state of input {self!r}")
        else:
            self._gpio.write(self._n, bool(value))

    # ---- pull ---------------------------------------------------------------------
    def _get_pull(self):
        return self._pull

    def _set_pull(self, value):
        if value not in _PULL_MODES:
            raise PinInvalidPull(f"invalid pull {value!r} for {self!r}")
        self._pull = value
        if self._function == "input":
            self._gpio.mode(self._n, _PULL_MODES[value])

    # ---- PWM -----------------------------------------------------------------------
    def _get_frequency(self):
        return self._frequency

    def _set_frequency(self, value):
        if value is None:
            if self._frequency is not None:
                self._pwm.detach(self._n)
                self._frequency = None
                if self._function == "output":
                    self._gpio.mode(self._n, "output")
                    self._gpio.write(self._n, False)
        else:
            self._pwm.attach(self._n, int(value), _PWM_RES_BITS)
            self._frequency = float(value)
            self._pwm.write(self._n, round(self._duty * _PWM_MAX))

    # ---- edge detection ----------------------------------------------------------------
    def _get_bounce(self):
        return self._bounce

    def _set_bounce(self, value):
        self._bounce = value
        self._rearm()

    def _get_edges(self):
        return self._edges

    def _set_edges(self, value):
        if value not in ("both", "rising", "falling", "none"):
            raise PinInvalidFunction(f"invalid edges {value!r}")
        self._edges = value
        self._rearm()

    def _get_when_changed(self):
        return self._when_changed

    def _set_when_changed(self, value):
        self._when_changed = value
        self._rearm()

    def _rearm(self):
        if self._watching:
            self._gpio.unwatch(self._n)
            self._watching = False
        if self._when_changed is not None and self._edges != "none":
            debounce_ms = int((self._bounce or 0) * 1000)
            self._gpio.watch(self._n, _EDGE_MAP[self._edges],
                             debounce_ms=debounce_ms, callback=self._on_edge)
            self._watching = True

    def _on_edge(self, event):
        cb = self._when_changed
        if cb is not None:
            cb(self._factory.ticks(), event.level)

    def close(self):
        try:
            if self._watching:
                self._gpio.unwatch(self._n)
                self._watching = False
            if self._frequency is not None:
                self._pwm.detach(self._n)
                self._frequency = None
            self._gpio.mode(self._n, "input")
        except Exception:
            pass  # ignore errors: the bridge may already be closed, e.g. during interpreter shutdown
        self._function = "input"


class EspBridgeFactory(Factory):
    """gpiozero pin factory backed by an espbridge Bridge."""

    def __init__(self, esp):
        super().__init__()
        self._esp = esp
        self.pins: dict = {}
        gpio_count = esp.info.gpio_count if esp.info is not None else 40
        chip = esp.info.chip.name if esp.info is not None else "ESP32"
        self._board_info = _board_info(gpio_count, chip)

    def _get_board_info(self):
        return self._board_info

    @property
    def board_info(self):
        return self._board_info

    def pin(self, name):
        for _header, info in self.board_info.find_pin(name):
            pin = self.pins.get(info)
            if pin is None:
                pin = EspBridgePin(self, info)
                self.pins[info] = pin
            return pin
        raise PinInvalidPin(f"{name} is not a valid pin name")

    def ticks(self):
        return time.monotonic()

    @staticmethod
    def ticks_diff(later, earlier):
        return later - earlier

    def close(self):
        for pin in list(self.pins.values()):
            pin.close()
        self.pins.clear()
