"""DHT decoder against synthetic pulse trains (no hardware)."""
import pytest

from espbridge.dht import DHT, convert, decode


def dht_pulses(data: bytes) -> list[tuple[int, int]]:
    """Build a realistic capture: trigger low, release, 80/80 response,
    40 bits (50 us low + 26/70 us high), stop low."""
    syms = [(0, 18_000), (1, 30), (0, 80), (1, 80)]
    for byte in data:
        for bit in range(7, -1, -1):
            syms.append((0, 50))
            syms.append((1, 70 if byte >> bit & 1 else 26))
    syms.append((0, 50))
    return syms


def payload(h10: int, t10: int) -> bytes:
    raw = bytes([h10 >> 8, h10 & 0xFF, t10 >> 8, t10 & 0xFF])
    return raw + bytes([sum(raw) & 0xFF])


def test_decode_dht22():
    data = payload(655, 231)  # 65.5 %RH, 23.1 C
    assert decode(dht_pulses(data)) == data
    temp, hum = convert(data, 22)
    assert (temp, hum) == (23.1, 65.5)


def test_decode_negative_temp():
    raw = bytes([0x02, 0x8C, 0x80, 0x65])  # -10.1 C, 65.2 %RH
    data = raw + bytes([sum(raw) & 0xFF])
    temp, hum = convert(decode(dht_pulses(data)), 22)
    assert temp == -10.1 and hum == 65.2


def test_decode_dht11():
    data = bytes([45, 0, 21, 5, 71])  # checksum 45+21+5=71
    temp, hum = convert(decode(dht_pulses(data)), 11)
    assert temp == 21.5 and hum == 45.0


def test_checksum_mismatch_raises():
    bad = payload(500, 250)[:-1] + b"\x00"
    with pytest.raises(ValueError, match="checksum"):
        decode(dht_pulses(bad))


def test_short_capture_raises():
    with pytest.raises(ValueError, match="short read"):
        decode([(0, 18_000), (1, 30)])


def test_read_via_bridge(bridge, fw):
    sensor = DHT(bridge, pin=4, model=22)
    fw.rmt_capture = dht_pulses(payload(421, 305))
    temp, hum = sensor.read()
    assert (temp, hum) == (30.5, 42.1)
    # the same-pin open-drain trigger was used
    assert fw.rmt_recv_args[0][4] == (4, 0, 18_000)
