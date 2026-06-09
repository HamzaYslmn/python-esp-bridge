"""MAX98357A — I2S class-D mono audio amplifier (output).

A thin, ergonomic wrapper over the bridge's I2S primitive (``esp.i2s``).

    from espbridge import Bridge

    with Bridge() as esp:
        amp = esp.max98357(bclk=26, ws=25, dout=22)   # == MAX98357(esp, ...)
        amp.tone(440, 1.0)                             # 1 s A4 beep
        amp.play(my_pcm16_bytes)                       # 16-bit LE PCM
        amp.stop()

I2S format (from the Analog Devices MAX98357A/B datasheet): the part accepts
standard **I2S (Philips) format** and supports 16/24/32-bit data. We drive the
common, link-friendly case: **16-bit LE PCM**. It needs no external MCLK. It is
a mono amp — its single channel is selected/summed by the SD-MODE strap (default
sums L+R / 2); we send a single mono stream (``stereo=False``).

Refs:
  https://www.analog.com/media/en/technical-documentation/data-sheets/MAX98357A-MAX98357B.pdf
"""
from __future__ import annotations

import math
import struct


class MAX98357:
    def __init__(self, bridge, *, bclk: int, ws: int, dout: int,
                 rate: int = 16_000, bits: int = 16, stereo: bool = False):
        self._i2s = bridge.i2s
        self._bclk = bclk
        self._ws = ws
        self._dout = dout
        self._rate = rate
        self._bits = bits
        self._stereo = stereo
        self._i2s.begin_output(bclk=bclk, ws=ws, dout=dout, rate=rate,
                               bits=bits, stereo=stereo)

    def play(self, pcm: bytes) -> int:
        """Play raw 16-bit little-endian PCM; returns bytes written."""
        return self._i2s.write(pcm)

    def tone(self, freq: float, seconds: float, volume: float = 0.5) -> int:
        """Generate and play a sine wave at `freq` Hz for `seconds`.

        Produces ``rate * seconds`` 16-bit signed samples scaled by
        ``volume`` (0..1), then writes them. Returns bytes written.
        """
        volume = max(0.0, min(1.0, volume))
        n = int(self._rate * seconds)
        amp = int(volume * 32767)
        step = 2.0 * math.pi * freq / self._rate
        samples = bytearray()
        for i in range(n):
            value = int(amp * math.sin(step * i))
            # clamp into signed 16-bit range, then pack little-endian
            value = max(-32768, min(32767, value))
            samples += struct.pack("<h", value)
        return self.play(bytes(samples))

    def stop(self) -> None:
        """Stop the I2S peripheral and release its pins."""
        self._i2s.end()
