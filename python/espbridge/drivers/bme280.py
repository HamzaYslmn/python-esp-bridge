"""BME280 — combined temperature / humidity / pressure sensor (also BMP280).

A "bring your own" I2C driver over the bridge's I2C primitive: reads the
factory trimming parameters, configures oversampling + normal mode, and applies
Bosch's compensation formulas to turn raw ADC counts into physical units.

    from espbridge import Bridge

    with Bridge() as esp:
        esp.i2c.init(sda=21, scl=22)
        s = esp.bme280(address=0x76)      # == BME280(esp, address=0x76)
        print(s.read())                   # {'temperature': 22.4, 'pressure': 1013.2, 'humidity': 41.8}
        print(s.temperature())            # 22.4  (degrees C)

The same part is sold on breakouts at 0x76 (SDO->GND) or 0x77 (SDO->VDD). A
BMP280 (chip id 0x58) shares the register map but has no humidity channel; this
driver detects it and returns humidity = None.

Compensation math and the calibration register layout are taken verbatim from
the Bosch BME280 datasheet (BST-BME280-DS002, rev 1.6), sections 4.2.2
"Trimming parameter readout" and 4.2.3 "Compensation formulas", cross-checked
against Bosch's reference driver (github.com/boschsensortec/BME280_SensorAPI).
The double-precision compensation variants are used (datasheet Appendix 8.1) —
they are clearer and accurate to the sensor's own resolution.
"""
from __future__ import annotations

from ..i2c import bind_i2c

import struct
import time

# --- Register map (datasheet section 5.3, "Memory map") -----------------------
_REG_ID = 0xD0          # chip id; reads 0x60 for BME280, 0x58 for BMP280
_REG_RESET = 0xE0       # write 0xB6 to soft-reset
_REG_CTRL_HUM = 0xF2    # osrs_h[2:0]; only takes effect after a ctrl_meas write
_REG_CTRL_MEAS = 0xF4   # osrs_t[7:5] | osrs_p[4:2] | mode[1:0]
_REG_CONFIG = 0xF5      # t_sb[7:5] | filter[4:2] | spi3w_en[0]
_REG_DATA = 0xF7        # press(0xF7..F9) temp(0xFA..FC) hum(0xFD..FE), 8 bytes

_REG_CALIB_TP = 0x88    # dig_T1..T3, dig_P1..P9, dig_H1: 26 bytes (0x88..0xA1)
_REG_CALIB_H = 0xE1     # dig_H2..H6: 7 bytes (0xE1..0xE7)

_RESET_CMD = 0xB6
_CHIP_ID_BME280 = 0x60
_CHIP_ID_BMP280 = 0x58

_MODE_NORMAL = 0b11     # ctrl_meas mode: continuous cycling measurements


class BME280:
    def __init__(self, bridge, address: int = 0x76, *, bus: int = 0,
                 sda: int | None = None, scl: int | None = None,
                 osrs_t: int = 1, osrs_p: int = 1, osrs_h: int = 1):
        for name, v in (("osrs_t", osrs_t), ("osrs_p", osrs_p), ("osrs_h", osrs_h)):
            if not 0 <= v <= 5:
                raise ValueError(f"{name} must be 0..5 (oversampling x1..x16; "
                                 f"0 skips the channel), got {v}")
        if not 0x76 <= address <= 0x77:
            raise ValueError(f"BME280 address {address:#04x} out of range "
                             f"(0x76-0x77)")
        self._i2c, self._addr, self._bus = bind_i2c(bridge, address, bus=bus, sda=sda, scl=scl)

        self.chip_id = self._i2c.read_reg(self._addr, _REG_ID, 1, self._bus)[0]
        if self.chip_id not in (_CHIP_ID_BME280, _CHIP_ID_BMP280):
            raise RuntimeError(f"no BME280/BMP280 at {address:#04x}: chip id "
                               f"{self.chip_id:#04x} (expected 0x60 or 0x58)")
        self.has_humidity = self.chip_id == _CHIP_ID_BME280

        self._t_fine = 0
        self._read_calibration()
        self._configure(osrs_t, osrs_p, osrs_h)

    # --- configuration --------------------------------------------------------
    def _configure(self, osrs_t: int, osrs_p: int, osrs_h: int) -> None:
        """Set oversampling and enter normal mode.

        Datasheet 5.4.3: ctrl_hum (0xF2) must be written *before* ctrl_meas
        (0xF4), because changes to osrs_h only become effective after a
        subsequent write to ctrl_meas.
        """
        if self.has_humidity:
            self._i2c.write_reg(self._addr, _REG_CTRL_HUM, osrs_h & 0x07, self._bus)
        ctrl_meas = ((osrs_t & 0x07) << 5) | ((osrs_p & 0x07) << 2) | _MODE_NORMAL
        self._i2c.write_reg(self._addr, _REG_CTRL_MEAS, ctrl_meas, self._bus)
        # config: t_sb=0, filter off, SPI 3-wire off (we use I2C).
        self._i2c.write_reg(self._addr, _REG_CONFIG, 0x00, self._bus)
        # First conversion in normal mode takes up to a few ms; give it a moment
        # so an immediate read() returns fresh data rather than the reset value.
        time.sleep(0.01)

    # --- calibration readout (datasheet 4.2.2, table 16) ----------------------
    def _read_calibration(self) -> None:
        # calib00..calib25 at 0x88..0xA1 (26 bytes): dig_T1..T3, dig_P1..P9,
        # then dig_H1 at 0xA1 (byte index 25; byte 24 at 0xA0 is reserved).
        tp = self._i2c.read_reg(self._addr, _REG_CALIB_TP, 26, self._bus)
        # Datasheet table 16 data types: T1 & P1 are unsigned short (u16 LE);
        # T2/T3 and P2..P9 are signed short (s16 LE); H1 is unsigned char.
        self.dig_T1 = struct.unpack_from("<H", tp, 0)[0]
        self.dig_T2 = struct.unpack_from("<h", tp, 2)[0]
        self.dig_T3 = struct.unpack_from("<h", tp, 4)[0]
        self.dig_P1 = struct.unpack_from("<H", tp, 6)[0]
        self.dig_P2 = struct.unpack_from("<h", tp, 8)[0]
        self.dig_P3 = struct.unpack_from("<h", tp, 10)[0]
        self.dig_P4 = struct.unpack_from("<h", tp, 12)[0]
        self.dig_P5 = struct.unpack_from("<h", tp, 14)[0]
        self.dig_P6 = struct.unpack_from("<h", tp, 16)[0]
        self.dig_P7 = struct.unpack_from("<h", tp, 18)[0]
        self.dig_P8 = struct.unpack_from("<h", tp, 20)[0]
        self.dig_P9 = struct.unpack_from("<h", tp, 22)[0]
        self.dig_H1 = tp[25]

        if not self.has_humidity:
            # BMP280: no humidity trimming registers, leave them None.
            self.dig_H2 = self.dig_H3 = self.dig_H4 = None
            self.dig_H5 = self.dig_H6 = None
            return

        # calib26..calib32 at 0xE1..0xE7 (7 bytes): dig_H2..H6.
        h = self._i2c.read_reg(self._addr, _REG_CALIB_H, 7, self._bus)
        # Datasheet table 16:
        #   dig_H2 = 0xE2<<8 | 0xE1  -> signed short
        #   dig_H3 = 0xE3            -> unsigned char
        #   dig_H4 = 0xE4<<4 | 0xE5[3:0]   (12-bit signed)
        #   dig_H5 = 0xE6<<4 | 0xE5[7:4]   (12-bit signed)
        #   dig_H6 = 0xE7           -> signed char
        # 0xE5 (byte index 4) is shared: its low nibble is dig_H4's LSBs and its
        # high nibble is dig_H5's LSBs.
        self.dig_H2 = struct.unpack_from("<h", h, 0)[0]
        self.dig_H3 = h[2]
        self.dig_H4 = (h[3] << 4) | (h[4] & 0x0F)
        self.dig_H5 = (h[5] << 4) | (h[4] >> 4)
        self.dig_H6 = struct.unpack_from("b", h, 6)[0]  # signed char

    # --- raw ADC readout ------------------------------------------------------
    def _read_raw(self) -> tuple[int, int, int | None]:
        """Burst-read the 8 data registers and unpack the raw ADC counts.

        Datasheet 4.1: pressure (0xF7..F9) and temperature (0xFA..FC) are 20-bit
        (MSB, LSB, then bits[7:4] of the XLSB byte); humidity (0xFD..FE) is
        16-bit (MSB, LSB). A single burst read keeps the three channels coherent.
        """
        d = self._i2c.read_reg(self._addr, _REG_DATA, 8, self._bus)
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
        adc_h = (d[6] << 8) | d[7] if self.has_humidity else None
        return adc_p, adc_t, adc_h

    # --- compensation (datasheet 4.2.3, double-precision variants) ------------
    def _compensate_temperature(self, adc_t: int) -> float:
        """Return temperature in degrees C and update t_fine.

        BME280_compensate_T_double (datasheet 8.1).
        """
        var1 = (adc_t / 16384.0 - self.dig_T1 / 1024.0) * self.dig_T2
        var2 = ((adc_t / 131072.0 - self.dig_T1 / 8192.0) ** 2) * self.dig_T3
        self._t_fine = var1 + var2
        return self._t_fine / 5120.0

    def _compensate_pressure(self, adc_p: int) -> float:
        """Return pressure in Pa. BME280_compensate_P_double (datasheet 8.1).

        Call _compensate_temperature first so t_fine is current.
        """
        var1 = self._t_fine / 2.0 - 64000.0
        var2 = var1 * var1 * self.dig_P6 / 32768.0
        var2 = var2 + var1 * self.dig_P5 * 2.0
        var2 = var2 / 4.0 + self.dig_P4 * 65536.0
        var1 = (self.dig_P3 * var1 * var1 / 524288.0 + self.dig_P2 * var1) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * self.dig_P1
        if var1 == 0.0:
            return 0.0  # avoid division by zero (datasheet returns 0)
        p = 1048576.0 - adc_p
        p = (p - var2 / 4096.0) * 6250.0 / var1
        var1 = self.dig_P9 * p * p / 2147483648.0
        var2 = p * self.dig_P8 / 32768.0
        return p + (var1 + var2 + self.dig_P7) / 16.0

    def _compensate_humidity(self, adc_h: int) -> float:
        """Return relative humidity in %RH. bme280_compensate_H_double (8.1).

        Call _compensate_temperature first so t_fine is current.
        """
        var_h = self._t_fine - 76800.0
        var_h = (adc_h - (self.dig_H4 * 64.0 + self.dig_H5 / 16384.0 * var_h)) * (
            self.dig_H2 / 65536.0 * (
                1.0 + self.dig_H6 / 67108864.0 * var_h * (
                    1.0 + self.dig_H3 / 67108864.0 * var_h)))
        var_h = var_h * (1.0 - self.dig_H1 * var_h / 524288.0)
        return max(0.0, min(100.0, var_h))

    # --- public API -----------------------------------------------------------
    def read(self) -> dict:
        """Take one measurement and return compensated values.

        Returns {"temperature": degC, "pressure": hPa, "humidity": %RH}.
        On a BMP280 (no humidity channel) "humidity" is None.
        """
        adc_p, adc_t, adc_h = self._read_raw()
        temperature = self._compensate_temperature(adc_t)   # sets t_fine first
        pressure = self._compensate_pressure(adc_p) / 100.0  # Pa -> hPa
        humidity = self._compensate_humidity(adc_h) if adc_h is not None else None
        return {"temperature": temperature, "pressure": pressure, "humidity": humidity}

    def temperature(self) -> float:
        """Temperature in degrees Celsius."""
        return self.read()["temperature"]

    def pressure(self) -> float:
        """Pressure in hPa (hectopascals / millibar)."""
        return self.read()["pressure"]

    def humidity(self) -> float | None:
        """Relative humidity in %RH (None on a BMP280)."""
        return self.read()["humidity"]

    def reset(self) -> None:
        """Issue a soft reset (datasheet 5.4.2); reconfigure before reading."""
        self._i2c.write_reg(self._addr, _REG_RESET, _RESET_CMD, self._bus)
        time.sleep(0.005)
