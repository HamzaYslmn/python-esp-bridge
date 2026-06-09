"""BH1750 — ambient light sensor (ROHM BH1750FVI), lux over I2C.

A "bring your own" driver over the bridge's I2C primitive. The BH1750 has no
register map: you send a single command byte to set the mode, then read back the
2-byte measurement.

    from espbridge import Bridge

    with Bridge() as esp:
        light = esp.bh1750(address=0x23)
        print(light.read_lux())           # e.g. 437.5

The ADDR pin selects the 7-bit address: low/GND = 0x23 (default), high/VDD =
0x5C. Commands (datasheet ROHM BH1750FVI Rev.D):
    0x00 POWER_DOWN
    0x01 POWER_ON
    0x07 RESET (clears the data register; only valid while powered on)
    0x10 CONTINUOUS  H-resolution  (1 lx,   ~120 ms)
    0x11 CONTINUOUS  H-resolution2 (0.5 lx, ~120 ms)
    0x13 CONTINUOUS  L-resolution  (4 lx,   ~16 ms)
    0x20 ONE_TIME    H-resolution
    0x21 ONE_TIME    H-resolution2
    0x23 ONE_TIME    L-resolution

Lux conversion (datasheet): lux = raw / 1.2 (the measurement accuracy factor).
In H-resolution2 mode the count is doubled, so divide by an extra 2.
"""
from __future__ import annotations

from ..i2c import init_if_pins

import time

POWER_DOWN = 0x00
POWER_ON = 0x01
RESET = 0x07

CONT_HIRES = 0x10
CONT_HIRES2 = 0x11
CONT_LORES = 0x13
ONE_HIRES = 0x20
ONE_HIRES2 = 0x21
ONE_LORES = 0x23

# Modes whose raw count is at 0.5 lx/count (twice the resolution); their lux
# value needs an extra /2.
_HIRES2_MODES = (CONT_HIRES2, ONE_HIRES2)


class BH1750:
    def __init__(self, bridge, address: int = 0x23, *, mode: int = CONT_HIRES,
                 bus: int = 0, sda: int | None = None, scl: int | None = None):
        if address not in (0x23, 0x5C):
            raise ValueError(f"BH1750 address {address:#04x} invalid "
                             f"(0x23 with ADDR low, 0x5C with ADDR high)")
        self._i2c = bridge.i2c
        self._addr = address
        self._bus = bus
        init_if_pins(self._i2c, bus=bus, sda=sda, scl=scl)
        self._mode = mode
        self.set_mode(mode)

    def _command(self, cmd: int) -> None:
        """Send a single command byte (the BH1750 has no register addressing)."""
        self._i2c.write(self._addr, bytes([cmd]), self._bus)

    def set_mode(self, mode: int) -> None:
        """Power on and select a measurement mode (one of the *_HIRES/LORES)."""
        self._mode = mode
        self._command(POWER_ON)
        self._command(mode)

    def read_lux(self) -> float:
        """Read the latest measurement and convert to lux.

        Waits the mode's typical integration time, then reads 2 bytes
        (big-endian) and applies lux = raw / 1.2 (and an extra /2 in the
        high-resolution-2 modes).
        """
        # H-res ~120 ms, L-res ~16 ms typical; wait the worst case plus margin.
        lores = self._mode in (CONT_LORES, ONE_LORES)
        time.sleep(0.024 if lores else 0.180)
        data = self._i2c.read(self._addr, 2, self._bus)
        raw = (data[0] << 8) | data[1]
        lux = raw / 1.2
        if self._mode in _HIRES2_MODES:
            lux /= 2.0
        return lux
