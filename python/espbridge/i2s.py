"""I2S audio in/out (std mode) — MAX98357 amps, INMP441/SPH0645 mics, ...

    esp.i2s.begin_output(bclk=26, ws=25, dout=22, rate=16_000)
    esp.i2s.write(pcm_bytes)                       # 16-bit LE PCM

    esp.i2s.begin_input(bclk=14, ws=15, din=32, rate=16_000)
    pcm = esp.i2s.read(seconds=2)

Bandwidth reality check: the link moves ~92 KB/s at 921600 baud (much less
over BLE). 16-bit mono fits up to ~32 kHz; 44.1 kHz stereo does NOT — use
lower rates or record to the ESP32's filesystem and fetch the file after.
"""
from __future__ import annotations

import struct

from . import constants as C

_CHUNK = C.MAX_PAYLOAD - 8


class I2s:
    def __init__(self, bridge):
        self._b = bridge
        bridge.require(C.Cap.I2S, "I2S")
        self._frame = 2  # bytes per sample-frame (bits/8 * channels); used by read(seconds=)
        self._rate = 16_000  # sample rate in Hz; updated by begin_output/begin_input

    def _begin(self, dir_: int, bclk: int, ws: int, dout: int, din: int,
               rate: int, bits: int, stereo: bool) -> None:
        if bits not in (8, 16, 24, 32):
            raise ValueError("bits must be 8/16/24/32")
        self._rate = rate
        self._frame = bits // 8 * (2 if stereo else 1)
        self._b.request(C.I2S_INIT,
                        struct.pack(">BbbbbIBB", dir_, bclk, ws, dout, din,
                                    rate, bits, 1 if stereo else 0))

    def begin_output(self, *, bclk: int, ws: int, dout: int,
                     rate: int = 16_000, bits: int = 16,
                     stereo: bool = False) -> None:
        """Speaker/amp path (e.g. MAX98357: bclk/ws/dout, gain strap)."""
        self._begin(1, bclk, ws, dout, -1, rate, bits, stereo)

    def begin_input(self, *, bclk: int, ws: int, din: int,
                    rate: int = 16_000, bits: int = 16,
                    stereo: bool = False) -> None:
        """Microphone path (e.g. INMP441: bclk/ws/din, L/R strap)."""
        self._begin(2, bclk, ws, -1, din, rate, bits, stereo)

    def write(self, pcm: bytes) -> int:
        """Play raw PCM (little-endian, interleaved when stereo); blocks as
        the DMA queue fills — that's the playback pacing."""
        written = 0
        for i in range(0, len(pcm), _CHUNK):
            chunk = pcm[i : i + _CHUNK]
            r = self._b.request(C.I2S_WRITE, chunk, timeout=10.0)
            w = struct.unpack(">H", r)[0]
            written += w
            if w < len(chunk):
                break  # firmware accepted fewer bytes than sent — DMA buffer is full, stop early
        return written

    def read(self, n: int | None = None, *, seconds: float | None = None) -> bytes:
        """Capture raw PCM: `n` bytes, or `seconds` worth at the begin rate."""
        if seconds is not None:
            n = int(seconds * self._rate) * self._frame
        if n is None:
            n = _CHUNK
        parts: list[bytes] = []
        got = 0
        while got < n:
            want = min(_CHUNK, n - got)
            chunk = self._b.request(C.I2S_READ, struct.pack(">H", want),
                                    timeout=10.0)
            if not chunk:
                break
            parts.append(chunk)
            got += len(chunk)
        return b"".join(parts)

    def end(self) -> None:
        self._b.request(C.I2S_DEINIT)
