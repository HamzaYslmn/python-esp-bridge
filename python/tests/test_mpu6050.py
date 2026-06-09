"""espbridge.drivers.mpu6050.MPU6050: wake-on-construct + sample decoding."""
import struct

import pytest

from espbridge.drivers.mpu6050 import MPU6050


class FakeI2c:
    def __init__(self, regs=None):
        self.inited = None
        self.writes = []                 # (addr, reg, value, bus) via write_reg
        self.reg_reads = []              # (addr, reg, n, bus)
        self._regs = regs or {}          # reg -> bytes returned by read_reg

    def init(self, *, sda=21, scl=22, freq=400_000, bus=0):
        self.inited = (sda, scl, bus)

    def write_reg(self, addr, reg, data, bus=0):
        value = data if isinstance(data, int) else bytes(data)
        self.writes.append((addr, reg, value, bus))

    def read_reg(self, addr, reg, n=1, bus=0):
        self.reg_reads.append((addr, reg, n, bus))
        return self._regs[reg]


class FakeEsp:
    def __init__(self, regs=None):
        self.i2c = FakeI2c(regs)


def test_construct_wakes_and_sets_ranges():
    esp = FakeEsp()
    MPU6050(esp)
    assert esp.i2c.inited is None                      # bus untouched, no pins
    # PWR_MGMT_1 (0x6B) <- 0x00 wakes the chip; ranges default to 0 (±2g/±250).
    assert esp.i2c.writes == [
        (0x68, 0x6B, 0x00, 0),
        (0x68, 0x1C, 0x00, 0),   # ACCEL_CONFIG, AFS_SEL=0
        (0x68, 0x1B, 0x00, 0),   # GYRO_CONFIG, FS_SEL=0
    ]


def test_range_bits_shifted_into_config():
    esp = FakeEsp()
    MPU6050(esp, accel_range=3, gyro_range=2)
    assert (0x68, 0x1C, 3 << 3, 0) in esp.i2c.writes   # ACCEL_CONFIG bits[4:3]
    assert (0x68, 0x1B, 2 << 3, 0) in esp.i2c.writes   # GYRO_CONFIG bits[4:3]


def test_who_am_i():
    esp = FakeEsp(regs={0x75: b"\x68"})
    assert MPU6050(esp).who_am_i() == 0x68


def _block(ax, ay, az, temp, gx, gy, gz):
    return struct.pack(">7h", ax, ay, az, temp, gx, gy, gz)


def test_read_accel_gyro_temp_decode():
    # ax raw 16384 -> 1.0 g at ±2g; gx raw 131 -> 1.0 dps at ±250;
    # temp raw such that raw/340 + 36.53 = 25.0  =>  raw = (25-36.53)*340.
    temp_raw = round((25.0 - 36.53) * 340.0)
    regs = {0x3B: _block(16384, -16384, 0, temp_raw, 131, -262, 0)}
    imu = MPU6050(FakeEsp(regs))

    ax, ay, az = imu.read_accel()
    assert ax == pytest.approx(1.0)
    assert ay == pytest.approx(-1.0)
    assert az == pytest.approx(0.0)

    gx, gy, gz = imu.read_gyro()
    assert gx == pytest.approx(1.0)
    assert gy == pytest.approx(-2.0)
    assert gz == pytest.approx(0.0)

    assert imu.read_temp() == pytest.approx(25.0, abs=0.01)


def test_read_all_one_burst_matches_individual_reads():
    temp_raw = round((25.0 - 36.53) * 340.0)
    regs = {0x3B: _block(16384, -16384, 0, temp_raw, 131, -262, 0)}
    imu = MPU6050(FakeEsp(regs))
    imu._i2c.reg_reads.clear()

    (accel, gyro, temp) = imu.read_all()
    # A single 14-byte burst read of ACCEL_XOUT_H services all three channels.
    assert imu._i2c.reg_reads == [(0x68, 0x3B, 14, 0)]
    assert accel == pytest.approx((1.0, -1.0, 0.0))
    assert gyro == pytest.approx((1.0, -2.0, 0.0))
    assert temp == pytest.approx(25.0, abs=0.01)


def test_init_with_pins_initialises_bus():
    esp = FakeEsp()
    MPU6050(esp, sda=4, scl=5, bus=1)
    assert esp.i2c.inited == (4, 5, 1)


def test_bad_args_rejected():
    with pytest.raises(ValueError):
        MPU6050(FakeEsp(), address=0x10)
    with pytest.raises(ValueError):
        MPU6050(FakeEsp(), accel_range=4)
    with pytest.raises(ValueError):
        MPU6050(FakeEsp(), gyro_range=-1)
