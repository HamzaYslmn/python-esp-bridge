"""I2S: init plumbing, chunked write/read."""
import pytest


def test_begin_output(bridge, fw):
    bridge.i2s.begin_output(bclk=26, ws=25, dout=22, rate=22_050)
    assert fw.i2s_config == (1, 26, 25, 22, -1, 22_050, 16, 0)


def test_begin_input_stereo(bridge, fw):
    bridge.i2s.begin_input(bclk=14, ws=15, din=32, rate=8_000, stereo=True)
    assert fw.i2s_config == (2, 14, 15, -1, 32, 8_000, 16, 1)


def test_write_chunks(bridge, fw):
    bridge.i2s.begin_output(bclk=26, ws=25, dout=22)
    pcm = bytes(5000)
    assert bridge.i2s.write(pcm) == 5000
    assert len(fw.i2s_written) == 5000


def test_read_by_seconds(bridge, fw):
    bridge.i2s.begin_input(bclk=14, ws=15, din=32, rate=1000)  # 2 B/frame
    fw.i2s_capture = bytes(range(256)) * 16  # 4096 B available
    pcm = bridge.i2s.read(seconds=1)  # 1000 frames * 2 B
    assert len(pcm) == 2000
    assert pcm == (bytes(range(256)) * 16)[:2000]


def test_bits_validated(bridge):
    with pytest.raises(ValueError):
        bridge.i2s.begin_output(bclk=1, ws=2, dout=3, bits=12)
