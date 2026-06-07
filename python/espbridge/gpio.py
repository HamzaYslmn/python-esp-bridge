"""GPIO: modes, read/write, batch ops, edge interrupts."""
from __future__ import annotations

import struct
from dataclasses import dataclass

from . import constants as C
from .errors import BridgeError

MODES = {
    "input": 0,
    "output": 1,
    "input_pullup": 2,
    "input_pulldown": 3,
    "output_open_drain": 4,
}
EDGES = {"rising": 1, "falling": 2, "change": 3}


@dataclass(frozen=True)
class EdgeEvent:
    pin: int
    level: int
    millis: int  # firmware uptime ms


class Gpio:
    def __init__(self, bridge):
        self._b = bridge
        self._watchers: dict[int, list] = {}
        bridge.on_event(C.GPIO_EDGE_EVT, self._on_edge)

    def _on_edge(self, payload: bytes) -> None:
        if len(payload) < 6:
            return
        ev = EdgeEvent(payload[0], payload[1], struct.unpack_from(">I", payload, 2)[0])
        for cb in self._watchers.get(ev.pin, ()):
            cb(ev)

    def mode(self, pin: int, mode: str | int) -> None:
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
        level = r[0] if r else want  # r is empty only on pre-read-back firmware
        if verify and level != want:
            raise BridgeError(
                f"GPIO{pin} read back {level} after writing {want}: pin not in "
                "output mode, shorted, or driven by another source?"
            )
        return level

    def read(self, pin: int) -> int:
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

    def watch(self, pin: int, edge: str = "change", debounce_ms: int = 0, callback=None) -> None:
        """Get `callback(EdgeEvent)` on pin edges (callback runs on the reader thread)."""
        e = EDGES[edge] if isinstance(edge, str) else int(edge)
        if callback is not None:
            self._watchers.setdefault(pin, []).append(callback)
        self._b.request(C.GPIO_WATCH, struct.pack(">BBH", pin, e, debounce_ms))

    def unwatch(self, pin: int) -> None:
        self._b.request(C.GPIO_UNWATCH, bytes([pin]))
        self._watchers.pop(pin, None)
