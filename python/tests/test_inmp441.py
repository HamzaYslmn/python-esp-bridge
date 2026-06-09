"""espbridge.drivers.inmp441.INMP441: 32-bit I2S capture + 16-bit downconvert."""
import struct

from espbridge.drivers.inmp441 import INMP441


class FakeI2s:
    def __init__(self, canned=b""):
        self.input_kwargs = None
        self.ended = False
        self.read_calls = []
        self._canned = canned

    def begin_input(self, **kwargs):
        self.input_kwargs = kwargs

    def read(self, n=None, *, seconds=None):
        self.read_calls.append((n, seconds))
        return self._canned

    def end(self):
        self.ended = True


class FakeEsp:
    def __init__(self, canned=b""):
        self.i2s = FakeI2s(canned)


def test_begin_input_uses_32bit_mono_with_given_pins_and_rate():
    esp = FakeEsp()
    INMP441(esp, bclk=14, ws=15, din=32, rate=22_050)
    assert esp.i2s.input_kwargs == dict(
        bclk=14, ws=15, din=32, rate=22_050, bits=32, stereo=False
    )


def test_record_returns_canned_bytes_via_seconds():
    canned = bytes(range(16))
    esp = FakeEsp(canned)
    mic = INMP441(esp, bclk=14, ws=15, din=32)
    assert mic.record(2) == canned
    assert esp.i2s.read_calls == [(None, 2)]


def test_record_16bit_takes_top_16_bits_of_each_32bit_slot():
    # Two 32-bit little-endian slots; top 16 bits = bytes [2:4].
    # slot A = 0x1234_5678 LE -> top16 = 0x1234 -> LE bytes 34 12
    # slot B = 0x7FFF_0000 LE -> top16 = 0x7FFF -> LE bytes FF 7F
    slot_a = struct.pack("<I", 0x12345678)
    slot_b = struct.pack("<I", 0x7FFF0000)
    esp = FakeEsp(slot_a + slot_b)
    mic = INMP441(esp, bclk=14, ws=15, din=32)
    out = mic.record_16bit(1)
    assert out == bytes([0x34, 0x12, 0xFF, 0x7F])


def test_stop_ends_i2s():
    esp = FakeEsp()
    mic = INMP441(esp, bclk=14, ws=15, din=32)
    mic.stop()
    assert esp.i2s.ended is True
