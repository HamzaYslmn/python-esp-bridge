"""Codec tests: CRC16, COBS, frame round-trips, splitter resync."""
import pytest

from espbridge.constants import MAX_PAYLOAD
from espbridge.errors import ProtocolError
from espbridge.protocol import (
    FrameSplitter,
    cobs_decode,
    cobs_encode,
    crc16_ccitt,
    decode_frame,
    encode_frame,
)


def test_crc16_known_vector():
    # CRC-16/CCITT-FALSE check value
    assert crc16_ccitt(b"123456789") == 0x29B1


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00",
        b"\x00\x00\x00",
        b"hello",
        b"a\x00b\x00c",
        bytes(range(256)),
        bytes(254),          # all zeros, block boundary
        b"\x01" * 254,       # max nonzero run
        b"\x01" * 255,
        b"\x01" * 1000 + b"\x00" + b"\x02" * 1000,
    ],
)
def test_cobs_roundtrip(data):
    encoded = cobs_encode(data)
    assert 0 not in encoded, "COBS output must not contain any zero byte (the frame delimiter)"
    assert cobs_decode(encoded) == data


def test_cobs_decode_rejects_embedded_zero():
    with pytest.raises(ProtocolError):
        cobs_decode(b"\x02a\x00b")


def test_cobs_decode_rejects_truncated_block():
    with pytest.raises(ProtocolError):
        cobs_decode(b"\x05ab")  # code promises 4 data bytes, only 2 present


def test_frame_roundtrip():
    wire = encode_frame(0, 7, 0x1003, b"\x01\x02\x00\x03")
    assert wire.endswith(b"\x00") and wire.count(b"\x00") == 1
    frame = decode_frame(wire[:-1])
    assert (frame.flags, frame.seq, frame.cmd, frame.payload) == (0, 7, 0x1003, b"\x01\x02\x00\x03")
    assert not frame.is_event and not frame.is_error


def test_frame_max_payload():
    payload = bytes(i & 0xFF for i in range(MAX_PAYLOAD))
    frame = decode_frame(encode_frame(1, 255, 0xFFFF, payload)[:-1])
    assert frame.payload == payload
    with pytest.raises(ProtocolError):
        encode_frame(0, 1, 0, bytes(MAX_PAYLOAD + 1))


def test_frame_crc_mismatch():
    wire = bytearray(encode_frame(0, 1, 0x0001, b"data"))
    wire[3] ^= 0xFF  # corrupt one body byte
    with pytest.raises(ProtocolError):
        decode_frame(bytes(wire[:-1]))


def test_frame_too_short():
    with pytest.raises(ProtocolError):
        decode_frame(cobs_encode(b"abc"))


def test_splitter_partial_feeds():
    wire = encode_frame(0, 1, 0x0001, b"abc")
    s = FrameSplitter()
    assert s.feed(wire[:3]) == []
    chunks = s.feed(wire[3:])
    assert len(chunks) == 1
    assert decode_frame(chunks[0]).payload == b"abc"


def test_splitter_multiple_frames_and_garbage_resync():
    f1 = encode_frame(0, 1, 0x0001, b"one")
    f2 = encode_frame(0, 2, 0x0002, b"two")
    s = FrameSplitter()
    # A garbage prefix (e.g. the ESP32 boot log before the firmware takes over)
    # is terminated by its own 0x00 byte and handed to decode_frame, which
    # correctly rejects it.  The two valid frames that follow still parse fine,
    # so the splitter re-syncs without any special handling.
    chunks = s.feed(b"\x07garbage\x00" + f1 + f2)
    assert len(chunks) == 3
    with pytest.raises(ProtocolError):
        decode_frame(chunks[0])
    assert decode_frame(chunks[1]).payload == b"one"
    assert decode_frame(chunks[2]).payload == b"two"
