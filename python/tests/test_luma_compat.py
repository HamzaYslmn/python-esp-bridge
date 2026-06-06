"""LumaI2C adapter: command/data framing and chunking (no luma needed)."""
from espbridge.compat.luma import LumaI2C


class FakeI2c:
    def __init__(self):
        self.writes = []

    def write(self, addr, data, bus=0):
        assert len(data) <= 2046, "exceeds firmware I2C_WRITE limit"
        self.writes.append((addr, bytes(data)))


def test_command_stream():
    i2c = FakeI2c()
    LumaI2C(i2c, addr=0x3C).command(0xAE, 0xD5, 0x80)
    assert i2c.writes == [(0x3C, bytes([0x00, 0xAE, 0xD5, 0x80]))]


def test_full_framebuffer_is_a_single_write():
    i2c = FakeI2c()
    fb = bytes(bytearray(range(256)) * 4)  # 1 KB framebuffer (128x64 SSD1306)
    LumaI2C(i2c, addr=0x3C).data(list(fb))  # luma passes lists of ints
    assert i2c.writes == [(0x3C, bytes([0x40]) + fb)]


def test_data_chunking_preserves_order():
    i2c = FakeI2c()
    fb = bytes(bytearray(range(256)) * 12)  # 3 KB > one I2C_WRITE
    LumaI2C(i2c, addr=0x3C).data(list(fb))
    assert all(w[1][0] == 0x40 for w in i2c.writes)
    assert b"".join(w[1][1:] for w in i2c.writes) == fb
    assert len(i2c.writes) == 2  # 2045 + 1027
