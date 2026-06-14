"""MPU6050 — 6-axis motion tracking (3-axis accelerometer + 3-axis gyroscope), I2C.

A "bring your own" driver over the bridge's I2C primitive (see docs/DRIVERS.md).

    from espbridge import Bridge

    with Bridge() as esp:
        imu = esp.mpu6050()                 # wakes the chip, ±2g / ±250 dps
        print(imu.who_am_i())               # 0x68
        print(imu.read_accel())             # (ax, ay, az) in g
        print(imu.read_gyro())              # (gx, gy, gz) in deg/s
        print(imu.read_temp())              # die temperature in °C

The MPU6500 and MPU9250 expose the *same* register map and decoding, so this
driver drives them too — only WHO_AM_I differs (0x70 for MPU6500, 0x71 for
MPU9250) so a strict who_am_i()==0x68 check will not pass on those parts.

Datasheet: InvenSense "MPU-6000/MPU-6050 Register Map and Descriptions".
Key registers (all addresses 7-bit, big-endian 16-bit sample words):
  SMPLRT_DIV   0x19  sample-rate divider
  CONFIG       0x1A  DLPF / external sync
  GYRO_CONFIG  0x1B  bits[4:3] FS_SEL  0=±250 1=±500 2=±1000 3=±2000 dps
  ACCEL_CONFIG 0x1C  bits[4:3] AFS_SEL 0=±2g  1=±4g  2=±8g   3=±16g
  ACCEL_XOUT_H 0x3B  start of the 14-byte burst: ax ay az temp gx gy gz
  PWR_MGMT_1   0x6B  write 0x00 to clear SLEEP and wake the device
  WHO_AM_I     0x75  returns 0x68 on a genuine MPU6050
"""
from __future__ import annotations

from ..i2c import bind_i2c

import struct

# Register addresses (see module docstring for the datasheet reference).
_CONFIG = 0x1A
_GYRO_CONFIG = 0x1B
_ACCEL_CONFIG = 0x1C
_ACCEL_XOUT_H = 0x3B
_PWR_MGMT_1 = 0x6B
_WHO_AM_I = 0x75

# FS_SEL / AFS_SEL value -> sensitivity (LSB per unit). ±2g => 16384 LSB/g,
# ±250 dps => 131 LSB/(deg/s); both halve for each step up in full-scale range.
_ACCEL_SCALE = {0: 16384.0, 1: 8192.0, 2: 4096.0, 3: 2048.0}   # LSB/g
_GYRO_SCALE = {0: 131.0, 1: 65.5, 2: 32.8, 3: 16.4}            # LSB/(deg/s)


class MPU6050:
    def __init__(self, bridge, address: int = 0x68, *, accel_range: int = 0,
                 gyro_range: int = 0, bus: int = 0,
                 sda: int | None = None, scl: int | None = None):
        if address not in (0x68, 0x69):
            raise ValueError(f"MPU6050 address {address:#04x} out of range "
                             f"(0x68, or 0x69 when AD0 is high)")
        if accel_range not in _ACCEL_SCALE:
            raise ValueError("accel_range must be 0..3 (±2/4/8/16 g)")
        if gyro_range not in _GYRO_SCALE:
            raise ValueError("gyro_range must be 0..3 (±250/500/1000/2000 dps)")
        self._i2c, self._addr, self._bus = bind_i2c(bridge, address, bus=bus, sda=sda, scl=scl)
        self._accel_scale = _ACCEL_SCALE[accel_range]
        self._gyro_scale = _GYRO_SCALE[gyro_range]
        # Clear SLEEP (PWR_MGMT_1 <- 0x00) so the sensors start sampling, then
        # program the full-scale ranges (bits[4:3] of the *_CONFIG registers).
        self._i2c.write_reg(self._addr, _PWR_MGMT_1, 0x00, self._bus)
        self._i2c.write_reg(self._addr, _ACCEL_CONFIG, accel_range << 3, self._bus)
        self._i2c.write_reg(self._addr, _GYRO_CONFIG, gyro_range << 3, self._bus)

    def who_am_i(self) -> int:
        """Read WHO_AM_I (0x75); 0x68 on a genuine MPU6050."""
        return self._i2c.read_reg(self._addr, _WHO_AM_I, 1, self._bus)[0]

    def read_raw(self) -> dict:
        """Burst-read all seven 16-bit samples (ACCEL_XOUT_H, 14 bytes).

        Returns a dict with raw signed int16s:
        ``ax ay az temp gx gy gz``. Words are big-endian per the datasheet.
        """
        block = self._i2c.read_reg(self._addr, _ACCEL_XOUT_H, 14, self._bus)
        ax, ay, az, temp, gx, gy, gz = struct.unpack(">7h", block)
        return {"ax": ax, "ay": ay, "az": az, "temp": temp,
                "gx": gx, "gy": gy, "gz": gz}

    def _decode(self, r: dict) -> tuple[tuple[float, float, float],
                                        tuple[float, float, float], float]:
        """Convert one raw-sample dict into (accel_g, gyro_dps, temp_C)."""
        a = self._accel_scale
        g = self._gyro_scale
        accel = (r["ax"] / a, r["ay"] / a, r["az"] / a)
        gyro = (r["gx"] / g, r["gy"] / g, r["gz"] / g)
        temp = r["temp"] / 340.0 + 36.53
        return accel, gyro, temp

    def read_all(self) -> tuple[tuple[float, float, float],
                                tuple[float, float, float], float]:
        """Read everything from ONE 14-byte burst -> (accel, gyro, temp).

        ``accel`` is (x, y, z) in g, ``gyro`` is (x, y, z) in deg/s, ``temp`` is
        the die temperature in °C. A single burst keeps all channels coherent.
        """
        return self._decode(self.read_raw())

    def read_accel(self) -> tuple[float, float, float]:
        """Acceleration (x, y, z) in g (raw / accel_scale)."""
        return self._decode(self.read_raw())[0]

    def read_gyro(self) -> tuple[float, float, float]:
        """Angular rate (x, y, z) in deg/s (raw / gyro_scale)."""
        return self._decode(self.read_raw())[1]

    def read_temp(self) -> float:
        """Die temperature in °C: raw/340 + 36.53 (datasheet formula)."""
        return self._decode(self.read_raw())[2]
