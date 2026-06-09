"""DS3231 — extremely accurate I2C real-time clock (TCXO), with temperature sensor.

A "bring your own" driver over the bridge's I2C primitive (see docs/DRIVERS.md).

    from espbridge import Bridge

    with Bridge() as esp:
        rtc = esp.ds3231()
        rtc.set(2026, 6, 9, 14, 30, 0, weekday=2)   # Tue 2026-06-09 14:30:00
        print(rtc.now())            # (2026, 6, 9, 14, 30, 0, 2)
        print(rtc.temperature())    # die temp in °C, 0.25° resolution

Datasheet: Analog Devices / Maxim DS3231. The I2C address is fixed at 0x68.
Timekeeping registers 0x00..0x06 are all BCD:
  0x00 seconds   0x01 minutes
  0x02 hours     bit6 = 12/24h select (kept 0 => 24-hour mode)
  0x03 weekday   (user-defined 1..7)
  0x04 day-of-month
  0x05 month     bit7 = century flag
  0x06 year      (00..99, offset from 2000)
Temperature (updated every 64 s):
  0x11 signed int8, whole degrees °C
  0x12 bits[7:6] = fractional quarter-degrees (0.25° per step)
"""
from __future__ import annotations

from ..i2c import bind_i2c

import struct

_ADDR = 0x68          # fixed I2C address of the DS3231
_REG_TIME = 0x00      # start of the 7-byte time/date block (all BCD)
_REG_TEMP = 0x11      # 0x11 = whole °C (signed), 0x12 = quarter-degrees in [7:6]


def _bcd_to_int(b: int) -> int:
    """Decode a packed binary-coded-decimal byte to an int (0x25 -> 25)."""
    return (b >> 4) * 10 + (b & 0x0F)


def _int_to_bcd(n: int) -> int:
    """Encode 0..99 to packed binary-coded decimal (25 -> 0x25)."""
    return ((n // 10) << 4) | (n % 10)


class DS3231:
    def __init__(self, bridge, address: int = _ADDR, *, bus: int = 0,
                 sda: int | None = None, scl: int | None = None):
        if address != _ADDR:
            raise ValueError(f"DS3231 address {address:#04x} invalid "
                             f"(fixed at 0x68)")
        self._i2c, self._addr, self._bus = bind_i2c(bridge, address, bus=bus, sda=sda, scl=scl)

    def now(self) -> tuple[int, int, int, int, int, int, int]:
        """Read the clock; returns (year, month, day, hour, minute, second, weekday).

        Year is the full year (2000 + the BCD year register).
        """
        b = self._i2c.read_reg(self._addr, _REG_TIME, 7, self._bus)
        second = _bcd_to_int(b[0] & 0x7F)
        minute = _bcd_to_int(b[1] & 0x7F)
        # 24-hour mode: mask off bit6 (12/24 select); the low 6 bits are the
        # tens+units of the hour.
        hour = _bcd_to_int(b[2] & 0x3F)
        weekday = _bcd_to_int(b[3] & 0x07)
        day = _bcd_to_int(b[4] & 0x3F)
        month = _bcd_to_int(b[5] & 0x1F)        # bit7 is the century flag
        year = 2000 + _bcd_to_int(b[6])
        return (year, month, day, hour, minute, second, weekday)

    def set(self, year: int, month: int, day: int, hour: int, minute: int,
            second: int, weekday: int = 1) -> None:
        """Set the clock (24-hour). Year may be full (2026) or 0..99.

        ``weekday`` is a user-defined 1..7 value the chip simply increments.
        """
        if year >= 2000:
            year -= 2000
        if not 0 <= year <= 99:
            raise ValueError("year must be 2000..2099 (or 0..99)")
        if not 1 <= month <= 12:
            raise ValueError("month must be 1..12")
        if not 1 <= day <= 31:
            raise ValueError("day must be 1..31")
        if not 0 <= hour <= 23:
            raise ValueError("hour must be 0..23 (24-hour mode)")
        if not 0 <= minute <= 59:
            raise ValueError("minute must be 0..59")
        if not 0 <= second <= 59:
            raise ValueError("second must be 0..59")
        if not 1 <= weekday <= 7:
            raise ValueError("weekday must be 1..7")
        block = bytes([
            _int_to_bcd(second),
            _int_to_bcd(minute),
            _int_to_bcd(hour),          # bit6 left 0 => 24-hour mode
            _int_to_bcd(weekday),
            _int_to_bcd(day),
            _int_to_bcd(month),         # bit7 (century) left 0
            _int_to_bcd(year),
        ])
        # Write the 7-byte block in one transfer starting at register 0x00.
        self._i2c.write(self._addr, bytes([_REG_TIME]) + block, self._bus)

    def temperature(self) -> float:
        """Die temperature in °C (0.25° resolution).

        0x11 is the signed whole-degree byte; 0x12 bits[7:6] add quarter-degrees.
        e.g. 0x1C, 0x40 -> 28 + 1*0.25 = 28.25 °C.
        """
        b = self._i2c.read_reg(self._addr, _REG_TEMP, 2, self._bus)
        whole = struct.unpack("b", bytes([b[0]]))[0]    # signed int8
        frac = (b[1] >> 6) * 0.25
        return whole + frac
