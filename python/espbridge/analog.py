"""ADC (oneshot reads), DAC (write + cosine generator), capacitive touch."""
from __future__ import annotations

import struct

from . import constants as C

# Attenuation -> roughly full-scale input voltage on classic ESP32:
#   0 dB ≈ 1.1 V, 2.5 dB ≈ 1.5 V, 6 dB ≈ 2.2 V, 11 dB ≈ 3.3 V (default)
ATTEN = {0: 0, 2.5: 1, 6: 2, 11: 3, "0db": 0, "2.5db": 1, "6db": 2, "11db": 3}


class Adc:
    def __init__(self, bridge):
        self._b = bridge

    def config(self, pin: int, atten=11) -> None:
        self._b.request(C.ADC_CONFIG, bytes([pin, ATTEN.get(atten, int(atten))]))

    def read(self, pin: int) -> int:
        """Raw 12-bit reading (0..4095)."""
        return struct.unpack(">H", self._b.request(C.ADC_READ, bytes([pin])))[0]

    def read_mv(self, pin: int) -> int:
        """Calibrated millivolts."""
        return struct.unpack(">H", self._b.request(C.ADC_READ_MV, bytes([pin])))[0]


class Dac:
    """True 8-bit DAC — classic ESP32 (GPIO 25/26) and S2 (GPIO 17/18) only."""

    def __init__(self, bridge):
        bridge.require(C.Cap.DAC, "DAC")
        self._b = bridge

    def write(self, pin: int, value: int) -> None:
        """Output value 0..255 (0..3.3 V)."""
        self._b.request(C.DAC_WRITE, bytes([pin, value & 0xFF]))

    def cosine(self, pin: int, freq_hz: int, *, scale: int = 0, offset: int = 0,
               phase_180: bool = False) -> None:
        """Start the hardware cosine-wave generator (~130 Hz .. ~100 kHz).

        scale: 0..3 = full/half/quarter/eighth amplitude.
        """
        self._b.request(C.DAC_COSINE, struct.pack(">BIBbB", pin, freq_hz, scale & 3,
                                                  offset, 1 if phase_180 else 0))

    def cosine_stop(self, pin: int) -> None:
        self._b.request(C.DAC_COS_STOP, bytes([pin]))

    def disable(self, pin: int) -> None:
        self._b.request(C.DAC_DISABLE, bytes([pin]))


class Touch:
    """Capacitive touch pads (classic ESP32: lower = touched; S2/S3: higher = touched)."""

    def __init__(self, bridge):
        bridge.require(C.Cap.TOUCH, "touch sensing")
        self._b = bridge

    def read(self, pin: int) -> int:
        return struct.unpack(">I", self._b.request(C.TOUCH_READ, bytes([pin])))[0]
