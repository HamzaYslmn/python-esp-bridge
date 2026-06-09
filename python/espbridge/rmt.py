"""Generic RMT pulse-train play/capture.

The firmware only moves symbols; device protocols live in Python on top of
this (espbridge.neopixel, .ir, .dht, .hcsr04, .stepper). A symbol is
``(level, ticks)`` with ticks in units of ``1/tick_hz`` seconds (1..0x7FFF).
"""
from __future__ import annotations

import struct

from . import constants as C

Symbol = tuple[int, int]  # (level 0/1, duration ticks)


def pack_symbols(symbols: list[Symbol]) -> bytes:
    out = bytearray()
    for level, ticks in symbols:
        if not 0 < ticks <= 0x7FFF:
            raise ValueError(f"symbol duration {ticks} out of range 1..32767")
        out += struct.pack(">H", (1 << 15 if level else 0) | ticks)
    return bytes(out)


def unpack_symbols(data: bytes) -> list[Symbol]:
    return [((s >> 15) & 1, s & 0x7FFF)
            for (s,) in struct.iter_unpack(">H", data)]


def _bit_sym(pair: tuple[Symbol, Symbol]) -> int:
    """One TX_BYTES bit definition: two symbols packed into a u32."""
    (l0, d0), (l1, d1) = pair
    return ((1 << 31 if l0 else 0) | (d0 & 0x7FFF) << 16
            | (1 << 15 if l1 else 0) | (d1 & 0x7FFF))


class Rmt:
    TX, RX = 0, 1

    def __init__(self, bridge):
        self._b = bridge
        bridge.require(C.Cap.RMT, "RMT")
        self._tick_hz: dict[int, int] = {}

    def init_tx(self, pin: int, tick_hz: int = 1_000_000) -> None:
        """Claim `pin` for transmit. tick_hz sets the symbol time base."""
        self._b.request(C.RMT_INIT, struct.pack(">BBI", pin, self.TX, tick_hz))
        self._tick_hz[pin] = tick_hz

    def init_rx(self, pin: int, tick_hz: int = 1_000_000) -> None:
        """Claim `pin` for receive."""
        self._b.request(C.RMT_INIT, struct.pack(">BBI", pin, self.RX, tick_hz))
        self._tick_hz[pin] = tick_hz

    def deinit(self, pin: int) -> None:
        self._b.request(C.RMT_DEINIT, bytes([pin]))
        self._tick_hz.pop(pin, None)

    def tx(self, pin: int, symbols: list[Symbol]) -> None:
        """Play a pulse train; blocks until it is on the wire."""
        payload = pack_symbols(symbols)
        secs = sum(t for _, t in symbols) / self._tick_hz.get(pin, 1_000_000)  # estimated wire time in seconds
        self._b.request(C.RMT_TX, bytes([pin]) + payload,
                        timeout=secs + self._b.timeout)

    def tx_bytes(self, pin: int, bit0: tuple[Symbol, Symbol],
                 bit1: tuple[Symbol, Symbol], data: bytes) -> None:
        """Expand `data` MSB-first into per-bit pulses (WS2812-style encoders).

        bit0/bit1 are each two symbols, e.g. ``((1, 4), (0, 9))`` = high 4
        ticks then low 9 ticks for a 0-bit.
        """
        if len(data) > C.MAX_PAYLOAD - 16:
            raise ValueError(f"at most {C.MAX_PAYLOAD - 16} bytes per tx_bytes call")
        bit_ticks = max(sum(t for _, t in bit0), sum(t for _, t in bit1))  # worst-case ticks per bit
        secs = len(data) * 8 * bit_ticks / self._tick_hz.get(pin, 1_000_000)
        self._b.request(C.RMT_TX_BYTES,
                        struct.pack(">BII", pin, _bit_sym(bit0), _bit_sym(bit1)) + data,
                        timeout=secs + self._b.timeout)

    def tx_loop(self, pin: int, symbols: list[Symbol]) -> None:
        """Continuously repeat the pulse train in hardware until tx_stop() is called.
        Useful for signals that must run indefinitely without host involvement,
        such as a stepper motor step clock or a continuous carrier."""
        self._b.request(C.RMT_TX_LOOP, bytes([pin]) + pack_symbols(symbols))

    def tx_stop(self, pin: int) -> None:
        self._b.request(C.RMT_TX_STOP, bytes([pin]))

    def recv(self, pin: int, *, idle_ticks: int, timeout_ms: int = 1000,
             max_symbols: int = C.RMT_MAX_RX_SYMS,
             trigger: tuple[int, int, int] | None = None) -> list[Symbol]:
        """Capture a pulse train; returns [] if nothing arrives in time.

        idle_ticks ends the capture once the line is quiet that long (must
        exceed the longest valid pulse). trigger=(pin, level, us) drives a
        start pulse with RX already armed — same pin (open-drain) for DHT,
        a separate pin for HC-SR04.
        """
        tpin, tlevel, tus = trigger if trigger else (0xFF, 0, 0)
        payload = struct.pack(">BHHHBBI", pin, idle_ticks, timeout_ms,
                              max_symbols, tpin, tlevel, tus)
        raw = self._b.request(C.RMT_RECV, payload,
                              timeout=timeout_ms / 1000 + self._b.timeout)
        return unpack_symbols(raw)

    def carrier(self, pin: int, freq: int, duty_pct: int = 33,
                enable: bool = True) -> None:
        """Modulate TX pulses with a carrier (38 kHz for IR)."""
        self._b.request(C.RMT_CARRIER,
                        struct.pack(">BIBB", pin, freq, duty_pct, 1 if enable else 0))
