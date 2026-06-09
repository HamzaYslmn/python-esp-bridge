"""WATCH — on-device, polled (non-interrupt) user-definable events.

Instead of polling a value from the host thousands of times, define a *rule* on
the ESP32: it samples a source on a background task and pushes a
:class:`WatchEvent` only when your condition trips. Nothing runs in an interrupt
service routine, so the firmware's main loop is never disturbed.

It's not analog-only — the *source* is whatever you pick:

    import espbridge
    esp = espbridge.connect()                 # USB or BLE — events arrive either way

    def on_trip(ev):
        print("value", ev.value, "active" if ev.active else "cleared")

    esp.watch.add("adc",  pin=34, above=3200, hysteresis=100, callback=on_trip)  # analog
    esp.watch.add("gpio", pin=15, equals=1, callback=on_trip)                    # digital
    esp.watch.add("touch", pin=4, below=40, callback=on_trip)                    # touch pad
    esp.watch.add("heap", change=20000, callback=on_trip)                        # free RAM

The callback runs on the bridge's reader thread. For ``await``-style code use
``async for ev in AsyncBridge.watch_events()`` (see :mod:`espbridge.aio`).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from . import constants as C
from .analog import ATTEN
from .errors import RemoteError, UnsupportedError

# source ids (must match mod_watch.cpp)
SOURCES = {"adc": 0, "adc_raw": 0, "adc_mv": 1, "gpio": 2, "touch": 3, "heap": 4}
# comparator ids
_ABOVE, _BELOW, _INSIDE, _OUTSIDE, _CHANGED = 0, 1, 2, 3, 4
# flag bits
_F_ENTER, _F_EXIT, _F_INITIAL = 0x01, 0x02, 0x04

# condition kwarg -> (comparator, a, b) from (value, hysteresis). One table so the
# accepted conditions, the error message and the encoding share a single source.
# equals=x is a degenerate band [x, x] (INSIDE) — exact match, ideal for digital.
_CONDITIONS = {
    "above":   lambda v, h: (_ABOVE,   int(v), int(v) - int(h)),
    "below":   lambda v, h: (_BELOW,   int(v), int(v) + int(h)),
    "equals":  lambda v, h: (_INSIDE,  int(v), int(v)),
    "between": lambda v, h: (_INSIDE,  int(v[0]), int(v[1])),
    "outside": lambda v, h: (_OUTSIDE, int(v[0]), int(v[1])),
    "change":  lambda v, h: (_CHANGED, int(v), 0),
}


@dataclass(frozen=True)
class WatchEvent:
    """One firing of a watch rule (see :meth:`Watch.add`)."""
    id: int
    state: int          # 1 = condition active / changed, 0 = condition cleared
    value: int          # the sampled value in the source's native units
    millis: int         # firmware uptime in ms when it fired

    @property
    def active(self) -> bool:
        """True when the rule's condition is currently met (state == 1)."""
        return bool(self.state)

    @classmethod
    def parse(cls, payload: bytes) -> "WatchEvent | None":
        """Decode a WATCH_EVT payload (id u8 | state u8 | value i32 | millis u32)."""
        if len(payload) < 10:
            return None
        wid, state = payload[0], payload[1]
        value, millis = struct.unpack_from(">iI", payload, 2)
        return cls(wid, state, value, millis)


class Watch:
    """Define on-device watch rules; their events arrive via callbacks.

    Each rule samples a source (``"adc"``, ``"adc_mv"``, ``"gpio"``, ``"touch"``,
    ``"heap"``) every ``period_ms`` and fires when a condition trips. Give the
    condition with exactly one of: ``above=``, ``below=``, ``between=(lo, hi)``,
    ``outside=(lo, hi)``, or ``change=delta``.
    """

    def __init__(self, bridge):
        self._b = bridge
        self._cbs: dict[int, list] = {}
        self._ids: set[int] = set()
        bridge.on_event(C.WATCH_EVT, self._on_event)

    def _on_event(self, payload: bytes) -> None:
        ev = WatchEvent.parse(payload)
        if ev is None:
            return
        for cb in self._cbs.get(ev.id, ()):
            cb(ev)

    def _alloc_id(self) -> int:
        for i in range(1, 256):
            if i not in self._ids:
                return i
        raise ValueError("too many watches (max 255 ids)")

    def add(self, source: str | int, *, pin: int | None = None,
            above: int | None = None, below: int | None = None,
            equals: int | None = None,
            between: tuple[int, int] | None = None,
            outside: tuple[int, int] | None = None,
            change: int | None = None,
            hysteresis: int = 0, period_ms: int = 100, atten=11,
            on_enter: bool = True, on_exit: bool = True, initial: bool = False,
            callback=None, id: int | None = None) -> int:
        """Register a rule on the board; returns its id (use it to :meth:`remove`).

        source:    what to sample — not just analog:
                     "adc"/"adc_raw" (0..4095), "adc_mv" (millivolts),
                     "gpio"  (digital level, 0/1),
                     "touch" (capacitive pad reading),
                     "heap"  (free bytes, no pin).
        condition: exactly one of:
                     above=x / below=x   — threshold (analog), hysteresis-aware
                     equals=x            — exact match; ideal for digital
                                           (equals=1 → pin high, equals=0 → low)
                     between=(lo, hi)    — value inside a band
                     outside=(lo, hi)    — value outside a band
                     change=delta        — value moved by >= delta since last event
        hysteresis: for above/below, how far the value must fall back before the
                   rule re-arms (suppresses chatter around the threshold).
        period_ms: how often the board samples (default 100 ms).
        on_enter/on_exit: emit when the condition becomes true / false again.
        initial:   also emit one event for the very first sample (current state).
        callback:  callback(WatchEvent), called on the reader thread when it fires.
        """
        src = SOURCES[source] if isinstance(source, str) else int(source)
        if src != SOURCES["heap"] and pin is None:
            raise ValueError(f"source {source!r} needs a pin=")

        given = {k: v for k, v in (("above", above), ("below", below),
                                   ("equals", equals), ("between", between),
                                   ("outside", outside), ("change", change))
                 if v is not None}
        if len(given) != 1:
            raise ValueError("give exactly one of " + "/".join(_CONDITIONS))
        (kind, val), = given.items()
        cmp, a, b = _CONDITIONS[kind](val, hysteresis)

        if id is None:
            id = self._alloc_id()
        flags = ((_F_ENTER if on_enter else 0) | (_F_EXIT if on_exit else 0)
                 | (_F_INITIAL if initial else 0))
        aux = ATTEN.get(atten, int(atten)) if src in (0, 1) else 0
        body = struct.pack(">BBBBBBHii", id, src, pin or 0, aux, cmp, flags,
                           int(period_ms), a, b)
        # Register the callback *before* arming the rule: the firmware can emit
        # its first event (e.g. initial=True) on the very next poll, which may
        # land before request() even returns — registering first avoids losing it.
        if callback is not None:
            self._cbs.setdefault(id, []).append(callback)
        self._ids.add(id)
        try:
            self._b.request(C.WATCH_ADD, body)
        except RemoteError as e:
            self._cbs.pop(id, None)
            self._ids.discard(id)
            if e.status == C.Status.UNKNOWN_CMD:
                raise UnsupportedError(
                    "watch engine needs bridge firmware >= 0.5.0 — reflash"
                ) from None
            raise
        return id

    def remove(self, id: int) -> None:
        """Remove one rule by its id (from the board and locally) and drop its callbacks."""
        self._b.request(C.WATCH_REMOVE, bytes([id]))
        self._ids.discard(id)
        self._cbs.pop(id, None)

    def clear(self) -> None:
        """Remove every rule (on the board and locally)."""
        self._b.request(C.WATCH_CLEAR)
        self._ids.clear()
        self._cbs.clear()

    def list(self) -> list[tuple[int, bool, int]]:
        """Snapshot of active rules from the board: ``(id, active, last_value)``."""
        r = self._b.request(C.WATCH_LIST)
        out, pos = [], 1
        for _ in range(r[0]):
            wid, state = r[pos], r[pos + 1]
            (value,) = struct.unpack_from(">i", r, pos + 2)
            pos += 6
            out.append((wid, bool(state), value))
        return out

    def on(self, id: int, callback) -> None:
        """Add another callback for an existing rule id."""
        self._cbs.setdefault(id, []).append(callback)
