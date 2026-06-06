"""CircuitPython/Blinka shims: busio-style I2C/SPI + digitalio DigitalInOut."""
from espbridge.compat.blinka import I2C, SPI, DigitalInOut, Direction, Pull


def test_i2c_protocol(bridge, fw):
    fw.i2c_devices[0x76] = bytes([0x60, 0x11, 0x22, 0x33])  # e.g. a BME280

    i2c = I2C(bridge)
    assert fw.i2c_inited
    assert i2c.scan() == [0x76]

    assert i2c.try_lock()
    assert not i2c.try_lock()  # busio semantics: non-blocking, exclusive

    i2c.writeto(0x76, bytearray([0xF4, 0x27]))
    assert fw.i2c_writes[-1] == (0x76, bytes([0xF4, 0x27]))

    buf = bytearray(3)
    i2c.readfrom_into(0x76, buf)
    assert bytes(buf) == bytes([0x60, 0x11, 0x22])

    out, in_ = bytearray([0xD0]), bytearray(1)
    i2c.writeto_then_readfrom(0x76, out, in_)
    assert fw.i2c_writes[-1] == (0x76, b"\xd0")
    assert in_[0] == 0x60  # chip-id style register read

    i2c.unlock()
    assert i2c.try_lock()


def test_i2c_buffer_slices(bridge, fw):
    fw.i2c_devices[0x40] = bytes(range(8))
    i2c = I2C(bridge)
    buf = bytearray(b"\xff" * 6)
    i2c.readfrom_into(0x40, buf, start=2, end=5)
    assert bytes(buf) == b"\xff\xff\x00\x01\x02\xff"


def test_spi_protocol(bridge, fw):
    spi = SPI(bridge)
    assert fw.spi_inited

    assert spi.try_lock()
    spi.configure(baudrate=1_000_000, polarity=0, phase=0)
    assert spi.frequency == 1_000_000

    spi.write(bytes([0x9F]))
    assert fw.spi_transfers[-1] == bytes([0x9F])

    buf = bytearray(3)
    spi.readinto(buf, write_value=0xAB)   # loopback fake echoes tx
    assert bytes(buf) == b"\xab\xab\xab"

    out, in_ = bytearray(b"hello"), bytearray(5)
    spi.write_readinto(out, in_)
    assert bytes(in_) == b"hello"
    spi.unlock()


def test_digitalinout(bridge, fw):
    cs = DigitalInOut(bridge, 5)
    assert cs.direction == Direction.INPUT  # CircuitPython default

    cs.switch_to_output(value=True)         # what SPIDevice does with CS
    assert cs.direction == Direction.OUTPUT
    assert fw.gpio_modes[5] == 1 and fw.gpio_levels[5] == 1
    cs.value = False
    assert fw.gpio_levels[5] == 0

    cs.switch_to_input(pull=Pull.UP)
    assert fw.gpio_modes[5] == 2            # input_pullup
    fw.gpio_levels[5] = 1
    assert cs.value is True
