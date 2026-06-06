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

KEY_MAX = 15  # NVS key-length limit


class Nvs:
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
        if isinstance(value, str):
            value = value.encode()
        elif isinstance(value, int):
            value = value.to_bytes(8, "big", signed=True)
        k = self._key(key)
        self._b.request(C.NVS_SET, bytes([len(k)]) + k + value)

    def get(self, key: str, default: bytes | None = None) -> bytes | None:
        try:
            return self._b.request(C.NVS_GET, self._key(key))
        except RemoteError as e:
            if e.status == C.Status.NOT_FOUND:
                return default
            raise

    def get_str(self, key: str, default: str | None = None) -> str | None:
        v = self.get(key)
        return default if v is None else v.decode()

    def get_int(self, key: str, default: int | None = None) -> int | None:
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
