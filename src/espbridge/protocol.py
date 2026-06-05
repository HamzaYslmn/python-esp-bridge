"""Frame codec: COBS framing + CRC-16/CCITT-FALSE.

Wire format (see docs/PROTOCOL.md):
    logical frame = flags u8 | seq u8 | cmd u16 BE | payload | crc16 BE
    on the wire   = COBS(logical) + b"\\x00"
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .constants import FLAG_ERROR, FLAG_EVENT, MAX_PAYLOAD
from .errors import ProtocolError

_HDR = struct.Struct(">BBH")

# ---- CRC-16/CCITT-FALSE (table-driven) ---------------------------------------


def _make_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)
    return table


_CRC_TABLE = _make_table()


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


# ---- COBS ----------------------------------------------------------------------


def cobs_encode(data: bytes) -> bytes:
    out = bytearray([0])  # placeholder for first code byte
    code_idx = 0
    code = 1
    for b in data:
        if b == 0:
            out[code_idx] = code
            code_idx = len(out)
            out.append(0)
            code = 1
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_idx] = code
                code_idx = len(out)
                out.append(0)
                code = 1
    out[code_idx] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        if code == 0:
            raise ProtocolError("zero byte inside COBS frame")
        i += 1
        end = i + code - 1
        if end > n:
            raise ProtocolError("truncated COBS block")
        out += data[i:end]
        i = end
        if code != 0xFF and i < n:
            out.append(0)
    return bytes(out)


# ---- frames ---------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    flags: int
    seq: int
    cmd: int
    payload: bytes

    @property
    def is_event(self) -> bool:
        return bool(self.flags & FLAG_EVENT)

    @property
    def is_error(self) -> bool:
        return bool(self.flags & FLAG_ERROR)


def encode_frame(flags: int, seq: int, cmd: int, payload: bytes = b"") -> bytes:
    """Logical frame -> wire bytes (COBS + 0x00 delimiter)."""
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError(f"payload too large: {len(payload)} > {MAX_PAYLOAD}")
    logical = _HDR.pack(flags, seq, cmd) + payload
    logical += struct.pack(">H", crc16_ccitt(logical))
    return cobs_encode(logical) + b"\x00"


def decode_frame(encoded: bytes) -> Frame:
    """Wire bytes between delimiters -> logical Frame. Raises ProtocolError."""
    logical = cobs_decode(encoded)
    if len(logical) < 6:
        raise ProtocolError(f"frame too short: {len(logical)} bytes")
    (crc,) = struct.unpack_from(">H", logical, len(logical) - 2)
    if crc16_ccitt(logical[:-2]) != crc:
        raise ProtocolError("CRC mismatch")
    flags, seq, cmd = _HDR.unpack_from(logical)
    return Frame(flags, seq, cmd, logical[4:-2])


# ---- wire-format helpers shared by the sub-API modules ---------------------------


def lp(s: str | bytes) -> bytes:
    """Length-prefixed string (len u8 | bytes) — the protocol's string format."""
    b = s.encode() if isinstance(s, str) else bytes(s)
    if len(b) > 255:
        raise ProtocolError(f"string too long for length prefix: {len(b)} bytes")
    return bytes([len(b)]) + b


def ip_str(b: bytes) -> str:
    """4 raw bytes -> dotted-quad string."""
    return ".".join(str(x) for x in b)


class FrameSplitter:
    """Accumulates raw serial bytes and yields COBS chunks between 0x00 delimiters."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        chunks: list[bytes] = []
        self._buf += data
        while True:
            i = self._buf.find(0)
            if i < 0:
                break
            if i > 0:
                chunks.append(bytes(self._buf[:i]))
            del self._buf[: i + 1]
        return chunks

    def reset(self) -> None:
        self._buf.clear()
