"""ADS1115 — 16-bit, 4-channel I2C analog-to-digital converter (TI).

A "bring your own" driver over the bridge's I2C primitive: single-shot reads of
the four single-ended inputs, decoded to volts. No firmware change needed.

    from espbridge import Bridge

    with Bridge() as esp:
        adc = esp.ads1115(address=0x48, gain=4.096)
        raw = adc.read(0)                 # signed 16-bit code on A0
        v   = adc.read_voltage(0)         # volts on A0

The ADDR pin selects the 7-bit address: GND=0x48 (default), VDD=0x49, SDA=0x4A,
SCL=0x4B. Two registers matter: Conversion (0x00, the last result, signed
big-endian) and Config (0x01, 16-bit big-endian).

Config register layout (datasheet TI SBAS444, Table 8):
    bit15    OS        1 = start a single conversion (write); 1 = idle (read)
    bit14:12 MUX       single-ended: A0=0b100 A1=0b101 A2=0b110 A3=0b111
                       differential: 0b000=A0-A1 0b001=A0-A3 0b010=A1-A3 0b011=A2-A3
    bit11:9  PGA       full-scale range (see _PGA below)
    bit8     MODE      1 = single-shot / power-down (default)
    bit7:5   DR        data rate, 0b100 = 128 SPS (default)
    bit4     COMP_MODE 0 = traditional comparator
    bit3     COMP_POL  0 = active low
    bit2     COMP_LAT  0 = non-latching
    bit1:0   COMP_QUE  0b11 = disable comparator, release ALERT/RDY (default)
"""
from __future__ import annotations

from ..i2c import init_if_pins

import struct
import time

_CONVERSION_REG = 0x00
_CONFIG_REG = 0x01

# Single-ended MUX codes (bits 14:12), indexed by channel 0..3.
_MUX_SINGLE = (0b100, 0b101, 0b110, 0b111)

# PGA: full-scale volts -> PGA[2:0] code (Config bits 11:9). 0b010 (+/-2.048 V)
# is the power-on default. Codes 0b101..0b111 all map to +/-0.256 V.
_PGA = {
    6.144: 0b000,
    4.096: 0b001,
    2.048: 0b010,
    1.024: 0b011,
    0.512: 0b100,
    0.256: 0b101,
}

# DR: samples-per-second -> DR[2:0] code (Config bits 7:5). 0b100 (128 SPS) is
# the power-on default.
_DR = {
    8: 0b000,
    16: 0b001,
    32: 0b010,
    64: 0b011,
    128: 0b100,
    250: 0b101,
    475: 0b110,
    860: 0b111,
}


class ADS1115:
    def __init__(self, bridge, address: int = 0x48, *, gain: float = 2.048,
                 data_rate: int = 128, bus: int = 0,
                 sda: int | None = None, scl: int | None = None):
        if gain not in _PGA:
            raise ValueError(f"ADS1115 gain (full-scale volts) must be one of "
                             f"{sorted(_PGA)}, got {gain}")
        if data_rate not in _DR:
            raise ValueError(f"ADS1115 data_rate (SPS) must be one of "
                             f"{sorted(_DR)}, got {data_rate}")
        if not 0x48 <= address <= 0x4B:
            raise ValueError(f"ADS1115 address {address:#04x} out of range "
                             f"(0x48-0x4B)")
        self._i2c = bridge.i2c
        self._addr = address
        self._bus = bus
        init_if_pins(self._i2c, bus=bus, sda=sda, scl=scl)
        self._gain = gain          # full-scale +/- volts for the chosen PGA
        self._data_rate = data_rate

    def _config(self, channel: int) -> int:
        """Build the 16-bit Config word for a single-shot read of `channel`."""
        if not 0 <= channel <= 3:
            raise ValueError(f"ADS1115 channel must be 0..3, got {channel}")
        return (
            (1 << 15)                       # OS = 1: start single conversion
            | (_MUX_SINGLE[channel] << 12)  # MUX: single-ended input
            | (_PGA[self._gain] << 9)       # PGA: full-scale range
            | (1 << 8)                      # MODE = 1: single-shot
            | (_DR[self._data_rate] << 5)   # DR: data rate
            | 0b11                          # COMP_QUE = 11: comparator off
        )

    def read(self, channel: int) -> int:
        """Sample one single-ended channel (0..3) -> signed 16-bit code."""
        cfg = self._config(channel)
        # Start the conversion: write Config big-endian.
        self._i2c.write_reg(self._addr, _CONFIG_REG, struct.pack(">H", cfg), self._bus)
        # Wait one conversion period plus margin (1/SPS + ~30%, min 1 ms).
        time.sleep(max(1.0 / self._data_rate * 1.3, 0.001))
        # Read the 16-bit result, signed big-endian.
        raw = self._i2c.read_reg(self._addr, _CONVERSION_REG, 2, self._bus)
        return struct.unpack(">h", raw)[0]

    def read_voltage(self, channel: int) -> float:
        """Sample one single-ended channel (0..3) -> volts.

        Full-scale codes span +/-32768, so volts = code / 32768 * full_scale.
        """
        return self.read(channel) / 32768.0 * self._gain
