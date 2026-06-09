"""Persistent key/value storage in the ESP32's NVS flash ("user" namespace).

    esp.nvs.set("boot_count", 7)
    esp.nvs.get_int("boot_count")     # 7
    esp.nvs.set("cfg", b"\\x01\\x02")
    esp.nvs.keys()                    # ['boot_count', 'cfg']

Values are bytes on the wire; set() encodes str (UTF-8) and int (8-byte
signed) for you, get_str/get_int decode.
"""
from __future__ import annotations

from . import constants as C
from .errors import RemoteError
from .protocol import lp

KEY_MAX = 15  # NVS key-length limit


class Nvs:
    """Persistent key/value store in NVS flash (see module docstring).

        esp.nvs.set("count", 5)
        esp.nvs.get_int("count")   # 5
    """

    def __init__(self, bridge):
        self._b = bridge
        bridge.require(C.Cap.NVS, "NVS")

    @staticmethod
    def _key(key: str) -> bytes:
        k = key.encode()
        if not 0 < len(k) <= KEY_MAX:
            raise ValueError(f"key must be 1..{KEY_MAX} bytes")
        return k

    def set(self, key: str, value: bytes | str | int) -> None:
        """Store a value; str is UTF-8 encoded, int as 8-byte signed big-endian."""
        if isinstance(value, str):
            value = value.encode()
        elif isinstance(value, int):
            value = value.to_bytes(8, "big", signed=True)
        self._b.request(C.NVS_SET, lp(self._key(key)) + value)

    def get(self, key: str, default: bytes | None = None) -> bytes | None:
        """Raw bytes for `key`, or `default` if it doesn't exist."""
        try:
            return self._b.request(C.NVS_GET, self._key(key))
        except RemoteError as e:
            if e.status == C.Status.NOT_FOUND:
                return default
            raise

    def get_str(self, key: str, default: str | None = None) -> str | None:
        """Value decoded as UTF-8, or `default` if the key is missing."""
        v = self.get(key)
        return default if v is None else v.decode()

    def get_int(self, key: str, default: int | None = None) -> int | None:
        """Value decoded as a signed big-endian int, or `default` if missing."""
        v = self.get(key)
        return default if v is None else int.from_bytes(v, "big", signed=True)

    def delete(self, key: str) -> bool:
        """-> False if the key didn't exist."""
        try:
            self._b.request(C.NVS_DEL, self._key(key))
            return True
        except RemoteError as e:
            if e.status == C.Status.NOT_FOUND:
                return False
            raise

    def keys(self) -> list[str]:
        """List every key currently stored in the user namespace."""
        raw = self._b.request(C.NVS_KEYS)
        out, pos = [], 1
        for _ in range(raw[0]):
            n = raw[pos]
            out.append(raw[pos + 1 : pos + 1 + n].decode())
            pos += 1 + n
        return out

    def clear(self) -> None:
        """Erase every key in the user namespace."""
        self._b.request(C.NVS_CLEAR)
