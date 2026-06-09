"""INMP441 — omnidirectional I2S MEMS microphone (input).

A thin, ergonomic wrapper over the bridge's I2S primitive (``esp.i2s``).

    from espbridge import Bridge

    with Bridge() as esp:
        mic = esp.inmp441(bclk=14, ws=15, din=32)   # == INMP441(esp, ...)
        raw = mic.record(2)                          # 2 s of raw 32-bit PCM
        pcm16 = mic.record_16bit(2)                  # 2 s as 16-bit LE PCM
        mic.stop()

I2S format (from the TDK InvenSense INMP441 datasheet): the part is an I2S
slave that outputs **24-bit two's-complement** samples, but it needs **32 SCK
cycles per data-word (64 per stereo WS frame)** — i.e. the 24 valid bits live in
the top of a 32-bit slot. We therefore always capture with **32-bit framing**.
The audio data is MSB-first and left-justified in the slot, so the
most-significant 16 bits of each 32-bit sample carry the usable audio (see
``record_16bit``).

The part is mono: its single L/R strap pin selects whether it drives the left
or right channel of the bus. We capture mono (``stereo=False``) — tie L/R to GND
so the mic sits on the left/first slot the driver reads.

Refs:
  https://www.farnell.com/datasheets/1824785.pdf  (INMP441 datasheet)
  https://www.digikey.com/htmldatasheets/production/1431884/0/0/1/inmp441-datasheet.html
"""
from __future__ import annotations

import struct


class INMP441:
    def __init__(self, bridge, *, bclk: int, ws: int, din: int,
                 rate: int = 16_000):
        # INMP441 packs its 24-bit sample into a 32-bit slot, so capture is
        # always forced to 32-bit mono frames.
        self._i2s = bridge.i2s
        self._bclk = bclk
        self._ws = ws
        self._din = din
        self._rate = rate
        self._i2s.begin_input(bclk=bclk, ws=ws, din=din, rate=rate,
                              bits=32, stereo=False)

    def record(self, seconds: float) -> bytes:
        """Capture `seconds` of raw PCM (32-bit LE slots, 24 valid MSB bits)."""
        return self._i2s.read(seconds=seconds)

    def record_16bit(self, seconds: float) -> bytes:
        """Capture `seconds` and downconvert to 16-bit little-endian PCM.

        Each 32-bit slot holds the audio left-justified (MSB-first), so the top
        16 bits already are a correctly-scaled signed 16-bit sample — we simply
        take the high 2 bytes of every 4-byte little-endian slot. This both
        truncates the 24-bit precision to 16 bits and halves the byte rate.
        """
        raw = self.record(seconds)
        out = bytearray()
        # Walk the little-endian 32-bit slots; bytes [2:4] are the top 16 bits.
        for i in range(0, len(raw) - 3, 4):
            out += raw[i + 2 : i + 4]
        return bytes(out)

    def stop(self) -> None:
        """Stop the I2S peripheral and release its pins."""
        self._i2s.end()
