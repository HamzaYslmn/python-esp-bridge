"""GPIO: modes, read/write, batch ops, edge interrupts."""
from __future__ import annotations

import struct
from dataclasses import dataclass

from . import constants as C
from .errors import BridgeError, RemoteError, needs_fw

MODES = {
    "input": 0,
    "output": 1,
    "input_pullup": 2,
    "input_pulldown": 3,
    "output_open_drain": 4,
}
_MODE_NAMES = {v: k for k, v in MODES.items()}
_OUTPUT_MODES = ("output", "output_open_drain")
EDGES = {"rising": 1, "falling": 2, "change": 3}


@dataclass(frozen=True)
class EdgeEvent:
    pin: int
    level: int
    millis: int  # firmware uptime ms

    @classmethod
    def parse(cls, payload: bytes) -> EdgeEvent | None:
        """Decode a GPIO_EDGE_EVT payload (pin u8 | level u8 | millis u32);
        None if it's too short to be a valid event."""
        if len(payload) < 6:
            return None
        return cls(payload[0], payload[1], struct.unpack_from(">I", payload, 2)[0])


@dataclass(frozen=True)
class PinStatus:
    """A pin's actual state, read from the chip (see Gpio.status)."""
    pin: int
    level: int               # actual pad level (0 or 1) read back from hardware
    mode: str | None         # mode last set via the bridge; None if never configured
    is_output: bool
    pwm_freq: int            # LEDC carrier frequency in Hz; 0 means no PWM on this pin
    pwm_duty: int            # LEDC raw duty count; 0 if no PWM

    @property
    def pwm(self) -> bool:
        """True when a PWM (LEDC) channel is driving this pin."""
        return self.pwm_freq > 0


def _pin_status(pin: int, level: int, mode: int, freq: int, duty: int) -> PinStatus:
    name = _MODE_NAMES.get(mode)
    return PinStatus(pin, level, name, name in _OUTPUT_MODES, freq, duty)


class Gpio:
    """Digital pins: modes, read/write, batch ops, edge interrupts.

        esp.gpio.mode(2, "output")
        esp.gpio.write(2, 1)
        esp.gpio.mode(15, "input_pullup")
        level = esp.gpio.read(15)
        esp.gpio.watch(15, "falling", callback=lambda ev: print(ev))
        esp.gpio.unwatch(15)
    """

    def __init__(self, bridge):
        self._b = bridge
        self._watchers: dict[int, list] = {}
        bridge.on_event(C.GPIO_EDGE_EVT, self._on_edge)

    def _on_edge(self, payload: bytes) -> None:
        ev = EdgeEvent.parse(payload)
        if ev is None:
            return
        for cb in self._watchers.get(ev.pin, ()):
            cb(ev)

    def mode(self, pin: int, mode: str | int) -> None:
        """Set a pin's mode: "input", "output", "input_pullup",
        "input_pulldown", or "output_open_drain"."""
        m = MODES[mode] if isinstance(mode, str) else int(mode)
        self._b.request(C.GPIO_SET_MODE, bytes([pin, m]))

    def write(self, pin: int, value: int | bool, *, verify: bool = False) -> int:
        """Set a pin; returns the level the firmware reads back as confirmation.

        The call already blocks for the firmware's ACK (it raises RemoteError /
        BridgeTimeoutError if the command didn't execute). The returned value is
        the pin's *actual* level read immediately after the write — for a normal
        push-pull output that echoes what you wrote; for open-drain or a pin not
        in output mode it reports the real pad state. With ``verify=True`` a
        mismatch (pin not an output, shorted, or driven by something else)
        raises BridgeError instead of returning silently.
        """
        want = 1 if value else 0
        r = self._b.request(C.GPIO_WRITE, bytes([pin, want]))
        level = r[0] if r else want  # older firmware returns an empty ACK; fall back to what we wrote
        if verify and level != want:
            raise BridgeError(
                f"GPIO{pin} read back {level} after writing {want}: pin not in "
                "output mode, shorted, or driven by another source?"
            )
        return level

    def read(self, pin: int) -> int:
        """Current level of a pin (0 or 1)."""
        return self._b.request(C.GPIO_READ, bytes([pin]))[0]

    def write_many(self, values: dict[int, int | bool]) -> None:
        """Set multiple output pins in one round-trip."""
        mask = vals = 0
        for pin, v in values.items():
            mask |= 1 << pin
            if v:
                vals |= 1 << pin
        self._b.request(C.GPIO_WRITE_MASK, struct.pack(">QQ", mask, vals))

    def read_all(self) -> int:
        """Levels of all pins as a bitmask (bit N = GPIO N)."""
        (levels,) = struct.unpack(">Q", self._b.request(C.GPIO_READ_ALL))
        return levels

    def status(self, pin: int) -> PinStatus:
        """Full state of a pin straight from the chip — not just "command ran":
        the live level, the mode it was configured with, and whether a PWM
        (LEDC) channel is driving it (with its frequency and duty)."""
        try:
            r = self._b.request(C.GPIO_STATUS, bytes([pin]))
        except RemoteError as e:
            needs_fw(e, "gpio.status() needs bridge firmware >= 0.3.6 — reflash")
        level, mode = r[0], r[1]
        freq, duty = struct.unpack_from(">II", r, 2)
        return _pin_status(pin, level, mode, freq, duty)

    def dump(self) -> list[PinStatus]:
        """Return the full status of every active pin in a single round-trip.
        Covers all pins that were configured via the bridge or are currently
        driven by PWM — useful for a complete snapshot without polling pin by pin."""
        try:
            r = self._b.request(C.GPIO_DUMP)
        except RemoteError as e:
            needs_fw(e, "gpio.dump() needs bridge firmware >= 0.3.7 — reflash")
        out, pos = [], 1
        for _ in range(r[0]):
            pin, mode, level = r[pos], r[pos + 1], r[pos + 2]
            freq, duty = struct.unpack_from(">II", r, pos + 3)
            pos += 11
            out.append(_pin_status(pin, level, mode, freq, duty))
        return out

    def watch(self, pin: int, edge: str = "change", debounce_ms: int = 0, callback=None) -> None:
        """Get `callback(EdgeEvent)` on pin edges (callback runs on the reader thread)."""
        e = EDGES[edge] if isinstance(edge, str) else int(edge)
        if callback is not None:
            self._watchers.setdefault(pin, []).append(callback)
        self._b.request(C.GPIO_WATCH, struct.pack(">BBH", pin, e, debounce_ms))

    def unwatch(self, pin: int) -> None:
        """Stop watching a pin and drop its callbacks."""
        self._b.request(C.GPIO_UNWATCH, bytes([pin]))
        self._watchers.pop(pin, None)
