"""ADC (oneshot reads), DAC (write + cosine generator), capacitive touch."""
from __future__ import annotations

import struct

from . import constants as C

# Attenuation level -> approximate full-scale input range on classic ESP32:
#   0 dB ≈ 1.1 V,  2.5 dB ≈ 1.5 V,  6 dB ≈ 2.2 V,  11 dB ≈ 3.3 V  (default)
ATTEN = {0: 0, 2.5: 1, 6: 2, 11: 3, "0db": 0, "2.5db": 1, "6db": 2, "11db": 3}


class Adc:
    """Oneshot ADC reads.

        esp.adc.config(34, atten=11)   # full ~3.3 V range
        raw = esp.adc.read(34)         # 0..4095
        mv = esp.adc.read_mv(34)       # calibrated millivolts
    """

    def __init__(self, bridge):
        self._b = bridge

    def config(self, pin: int, atten=11) -> None:
        """Set the input attenuation for a pin (0/2.5/6/11 dB; default 11 ≈ 3.3 V)."""
        self._b.request(C.ADC_CONFIG, bytes([pin, ATTEN.get(atten, int(atten))]))

    def read(self, pin: int) -> int:
        """Raw 12-bit reading (0..4095)."""
        return struct.unpack(">H", self._b.request(C.ADC_READ, bytes([pin])))[0]

    def read_mv(self, pin: int) -> int:
        """Calibrated millivolts."""
        return struct.unpack(">H", self._b.request(C.ADC_READ_MV, bytes([pin])))[0]


class Dac:
    """True 8-bit DAC — classic ESP32 (GPIO 25/26) and S2 (GPIO 17/18) only.

        esp.dac.write(25, 128)            # ~1.65 V
        esp.dac.cosine(25, 1000)          # 1 kHz cosine wave
        esp.dac.cosine_stop(25)
        esp.dac.disable(25)
    """

    def __init__(self, bridge):
        bridge.require(C.Cap.DAC, "DAC")
        self._b = bridge

    def write(self, pin: int, value: int) -> None:
        """Output value 0..255 (0..3.3 V)."""
        self._b.request(C.DAC_WRITE, bytes([pin, value & 0xFF]))

    def cosine(self, pin: int, freq_hz: int, *, scale: int = 0, offset: int = 0,
               phase_180: bool = False) -> None:
        """Start the hardware cosine-wave generator (~130 Hz .. ~100 kHz).

        scale: 0 = full amplitude, 1 = half, 2 = quarter, 3 = eighth.
        """
        self._b.request(C.DAC_COSINE, struct.pack(">BIBbB", pin, freq_hz, scale & 3,
                                                  offset, 1 if phase_180 else 0))

    def cosine_stop(self, pin: int) -> None:
        """Stop the cosine generator on a pin (the pin stays a DAC output)."""
        self._b.request(C.DAC_COS_STOP, bytes([pin]))

    def disable(self, pin: int) -> None:
        """Turn the DAC off on a pin and release it."""
        self._b.request(C.DAC_DISABLE, bytes([pin]))


class Touch:
    """Capacitive touch pads (classic ESP32: lower = touched; S2/S3: higher = touched).

        val = esp.touch.read(4)           # raw pad reading
    """

    def __init__(self, bridge):
        bridge.require(C.Cap.TOUCH, "touch sensing")
        self._b = bridge

    def read(self, pin: int) -> int:
        """Raw capacitive reading for a touch pad."""
        return struct.unpack(">I", self._b.request(C.TOUCH_READ, bytes([pin])))[0]
