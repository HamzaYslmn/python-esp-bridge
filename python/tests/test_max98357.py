"""espbridge.drivers.max98357.MAX98357: I2S output, play passthrough + tone gen."""
from espbridge.drivers.max98357 import MAX98357


class FakeI2s:
    def __init__(self):
        self.output_kwargs = None
        self.ended = False
        self.writes = []

    def begin_output(self, **kwargs):
        self.output_kwargs = kwargs

    def write(self, pcm):
        self.writes.append(bytes(pcm))
        return len(pcm)

    def end(self):
        self.ended = True


class FakeEsp:
    def __init__(self):
        self.i2s = FakeI2s()


def test_begin_output_uses_given_pins_rate_bits():
    esp = FakeEsp()
    MAX98357(esp, bclk=26, ws=25, dout=22, rate=8_000)
    assert esp.i2s.output_kwargs == dict(
        bclk=26, ws=25, dout=22, rate=8_000, bits=16, stereo=False
    )


def test_play_forwards_bytes():
    esp = FakeEsp()
    amp = MAX98357(esp, bclk=26, ws=25, dout=22)
    pcm = bytes([1, 2, 3, 4])
    n = amp.play(pcm)
    assert esp.i2s.writes == [pcm]
    assert n == 4


def test_tone_writes_rate_times_seconds_16bit_samples():
    esp = FakeEsp()
    rate = 8_000
    amp = MAX98357(esp, bclk=26, ws=25, dout=22, rate=rate)
    written = amp.tone(440, 0.5)
    expected_samples = int(rate * 0.5)            # 4000 samples
    assert len(esp.i2s.writes) == 1
    pcm = esp.i2s.writes[0]
    assert len(pcm) == expected_samples * 2       # 2 bytes per 16-bit sample
    assert written == len(pcm)


def test_stop_ends_i2s():
    esp = FakeEsp()
    amp = MAX98357(esp, bclk=26, ws=25, dout=22)
    amp.stop()
    assert esp.i2s.ended is True
