"""NeoPixel driver: pixel buffer, color order, brightness -> TX_BYTES payload."""
import pytest

from espbridge.neopixel import NeoPixel


def test_show_sends_grb(bridge, fw):
    strip = NeoPixel(bridge, pin=5, n=2)
    strip[0] = (255, 10, 20)
    strip[1] = (1, 2, 3)
    strip.show()
    pin, bit0, bit1, data = fw.rmt_tx_bytes[-1]
    assert pin == 5
    assert data == bytes([10, 255, 20, 2, 1, 3])  # GRB order
    # WS2812B @ 100 ns ticks: bit0 = 0.4us H + 0.9us L, bit1 = 0.8us H + 0.5us L
    assert bit0 == (1 << 31 | 4 << 16 | 9)
    assert bit1 == (1 << 31 | 8 << 16 | 5)


def test_rgb_order_and_getitem(bridge, fw):
    strip = NeoPixel(bridge, pin=5, n=1, order="RGB")
    strip[0] = (9, 8, 7)
    assert strip[0] == (9, 8, 7)
    strip.show()
    assert fw.rmt_tx_bytes[-1][3] == bytes([9, 8, 7])


def test_brightness_scales(bridge, fw):
    strip = NeoPixel(bridge, pin=5, n=1, brightness=0.5)
    strip[0] = (200, 100, 0)
    strip.show()
    assert fw.rmt_tx_bytes[-1][3] == bytes([50, 100, 0])  # GRB, halved


def test_fill_and_negative_index(bridge, fw):
    strip = NeoPixel(bridge, pin=5, n=3)
    strip.fill((1, 2, 3))
    strip[-1] = (4, 5, 6)
    assert strip[2] == (4, 5, 6)
    assert strip[0] == (1, 2, 3)


def test_too_many_pixels_rejected(bridge):
    with pytest.raises(ValueError):
        NeoPixel(bridge, pin=5, n=700)
