"""WS2812/SK6812 addressable LED strips, driven through the RMT primitive.

    from espbridge import Bridge
    from espbridge.drivers.neopixel import NeoPixel

    with Bridge() as esp:
        strip = NeoPixel(esp, pin=5, n=30)
        strip[0] = (255, 0, 0)
        strip.fill((0, 0, 32))
        strip.show()
"""
from __future__ import annotations

from .. import constants as C

# 100 ns ticks; WS2812B timing: 0-bit = 0.4 us high + 0.85 us low,
# 1-bit = 0.8 us high + 0.45 us low (the >50 us latch happens naturally
# once the line idles after the frame).
_TICK_HZ = 10_000_000
_BIT0 = ((1, 4), (0, 9))
_BIT1 = ((1, 8), (0, 5))

_ORDERS = {"RGB": (0, 1, 2), "GRB": (1, 0, 2), "BGR": (2, 1, 0),
           "RGBW": (0, 1, 2, 3), "GRBW": (1, 0, 2, 3)}


class NeoPixel:
    def __init__(self, bridge, pin: int, n: int, *, order: str = "GRB",
                 brightness: float = 1.0):
        self._rmt = bridge.rmt
        self._pin = pin
        self._order = _ORDERS[order.upper()]
        self.n = n
        self.brightness = brightness
        bpp = len(self._order)
        if n * bpp > C.MAX_PAYLOAD - 16:
            raise ValueError(f"at most {(C.MAX_PAYLOAD - 16) // bpp} pixels "
                             f"per strip ({order})")
        self._buf = bytearray(n * bpp)
        self._scale: bytes | None = None  # brightness LUT, built on first show()
        self._scale_for = -1.0
        self._rmt.init_tx(pin, _TICK_HZ)

    def __len__(self) -> int:
        return self.n

    def __setitem__(self, i: int, color: tuple[int, ...]) -> None:
        bpp = len(self._order)
        base = range(self.n)[i] * bpp  # range() normalizes negative indices
        for slot, chan in enumerate(self._order):
            self._buf[base + slot] = color[chan]

    def __getitem__(self, i: int) -> tuple[int, ...]:
        bpp = len(self._order)
        base = range(self.n)[i] * bpp
        px = self._buf[base : base + bpp]
        out = [0] * bpp
        for slot, chan in enumerate(self._order):
            out[chan] = px[slot]
        return tuple(out)

    def fill(self, color: tuple[int, ...]) -> None:
        for i in range(self.n):
            self[i] = color

    def clear(self) -> None:
        self._buf[:] = bytes(len(self._buf))
        self.show()

    def show(self) -> None:
        """Push the pixel buffer to the strip."""
        b = max(0.0, min(1.0, self.brightness))
        if b >= 1.0:
            data = bytes(self._buf)
        else:
            if b != self._scale_for:  # LUT rebuilt only when brightness changes
                self._scale = bytes(round(i * b) for i in range(256))
                self._scale_for = b
            data = self._buf.translate(self._scale)
        self._rmt.tx_bytes(self._pin, _BIT0, _BIT1, data)

    def deinit(self) -> None:
        self._rmt.deinit(self._pin)
