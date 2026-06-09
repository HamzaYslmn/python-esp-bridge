"""smbus2-compatible SMBus shim."""
import pytest

from espbridge.compat.smbus import SMBus
from espbridge.errors import RemoteError


@pytest.fixture()
def bus(bridge, fw):
    fw.i2c_devices[0x48] = bytes([0x1A, 0x80, 0x42, 0x43, 0x44])
    return SMBus(bridge)


def test_byte_ops(bus, fw):
    assert bus.read_byte(0x48) == 0x1A
    bus.write_byte(0x48, 0x55)
    assert fw.i2c_writes[-1] == (0x48, b"\x55")
    bus.write_quick(0x48)
    assert fw.i2c_writes[-1] == (0x48, b"")


def test_register_ops(bus, fw):
    assert bus.read_byte_data(0x48, 0x00) == 0x1A
    assert fw.i2c_writes[-1] == (0x48, b"\x00")  # read_byte_data first writes the register address, then reads

    bus.write_byte_data(0x48, 0x01, 0x60)
    assert fw.i2c_writes[-1] == (0x48, bytes([0x01, 0x60]))

    # SMBus words are little-endian
    assert bus.read_word_data(0x48, 0x00) == 0x801A
    bus.write_word_data(0x48, 0x02, 0xBEEF)
    assert fw.i2c_writes[-1] == (0x48, bytes([0x02, 0xEF, 0xBE]))


def test_block_ops(bus, fw):
    assert bus.read_i2c_block_data(0x48, 0x00, 5) == [0x1A, 0x80, 0x42, 0x43, 0x44]
    bus.write_i2c_block_data(0x48, 0x06, [1, 2, 3])
    assert fw.i2c_writes[-1] == (0x48, bytes([0x06, 1, 2, 3]))


def test_missing_device_raises(bus):
    with pytest.raises(RemoteError):
        bus.read_byte(0x21)
