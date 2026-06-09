"""Infrared remote send/receive (NEC protocol + raw) over the RMT primitive.

    from espbridge.drivers.ir import IrSender, IrReceiver
    IrSender(esp, pin=4).send_nec(addr=0x00, command=0x45)
    addr, cmd = IrReceiver(esp, pin=15).receive()   # TSOP-style 38 kHz module
"""
from __future__ import annotations

from ..rmt import Symbol

_TOL = 0.30  # +-30% pulse-width tolerance


def _near(dur: int, target: int) -> bool:
    return abs(dur - target) <= target * _TOL


def nec_symbols(addr: int, command: int) -> list[Symbol]:
    """Pure encoder: NEC frame as 1 us RMT symbols (carrier added by RMT)."""
    syms: list[Symbol] = [(1, 9000), (0, 4500)]
    for byte in (addr & 0xFF, ~addr & 0xFF, command & 0xFF, ~command & 0xFF):
        for bit in range(8):  # LSB first
            syms.append((1, 560))
            syms.append((0, 1690 if byte >> bit & 1 else 560))
    syms.append((1, 560))  # final mark
    return syms


def decode_nec(symbols: list[Symbol]) -> tuple[int, int] | str | None:
    """Pure decoder -> (addr, command), "repeat", or None.

    Works on durations only: TSOP receivers invert the line, so levels are
    unreliable but mark/space lengths are not.
    """
    durs = [d for _, d in symbols]
    if len(durs) >= 3 and _near(durs[0], 9000) and _near(durs[1], 2250):
        return "repeat"
    if len(durs) < 2 + 64 + 1 or not (_near(durs[0], 9000) and _near(durs[1], 4500)):
        return None
    value = 0
    for i in range(32):
        mark, space = durs[2 + 2 * i], durs[3 + 2 * i]
        if not _near(mark, 560):
            return None
        if _near(space, 1690):
            value |= 1 << i
        elif not _near(space, 560):
            return None
    addr, naddr = value & 0xFF, value >> 8 & 0xFF
    cmd, ncmd = value >> 16 & 0xFF, value >> 24 & 0xFF
    if cmd ^ ncmd != 0xFF:
        return None
    return (addr if addr ^ naddr == 0xFF else value & 0xFFFF, cmd)


class IrSender:
    def __init__(self, bridge, pin: int, carrier_hz: int = 38_000):
        self._rmt = bridge.rmt
        self._pin = pin
        self._rmt.init_tx(pin, 1_000_000)
        self._rmt.carrier(pin, carrier_hz, duty_pct=33)

    def send_nec(self, addr: int, command: int) -> None:
        self._rmt.tx(self._pin, nec_symbols(addr, command))

    def send_raw(self, symbols: list[Symbol]) -> None:
        self._rmt.tx(self._pin, symbols)

    def deinit(self) -> None:
        self._rmt.deinit(self._pin)


class IrReceiver:
    def __init__(self, bridge, pin: int):
        self._rmt = bridge.rmt
        self._pin = pin
        self._rmt.init_rx(pin, 1_000_000)

    def receive_raw(self, timeout_ms: int = 5000) -> list[Symbol]:
        """Wait for one IR frame; idle 12 ms ends it (largest NEC gap is 9 ms)."""
        return self._rmt.recv(self._pin, idle_ticks=12_000,
                              timeout_ms=timeout_ms, max_symbols=512)

    def receive(self, timeout_ms: int = 5000) -> tuple[int, int] | str | None:
        """-> (addr, command), "repeat", or None on timeout/non-NEC."""
        syms = self.receive_raw(timeout_ms)
        return decode_nec(syms) if syms else None

    def deinit(self) -> None:
        self._rmt.deinit(self._pin)
