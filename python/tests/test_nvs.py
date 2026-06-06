"""NVS key/value store."""
import pytest

from espbridge import constants as C


def test_set_get_bytes(bridge, fw):
    bridge.nvs.set("cfg", b"\x01\x02\x03")
    assert bridge.nvs.get("cfg") == b"\x01\x02\x03"
    assert fw.nvs == {"cfg": b"\x01\x02\x03"}


def test_str_and_int_helpers(bridge):
    bridge.nvs.set("name", "kiosk-3")
    bridge.nvs.set("count", -42)
    assert bridge.nvs.get_str("name") == "kiosk-3"
    assert bridge.nvs.get_int("count") == -42


def test_get_missing_returns_default(bridge):
    assert bridge.nvs.get("nope") is None
    assert bridge.nvs.get("nope", b"x") == b"x"
    assert bridge.nvs.get_int("nope", 7) == 7


def test_delete(bridge, fw):
    bridge.nvs.set("k", b"v")
    assert bridge.nvs.delete("k") is True
    assert bridge.nvs.delete("k") is False
    assert fw.nvs == {}


def test_keys_and_clear(bridge):
    bridge.nvs.set("a", b"1")
    bridge.nvs.set("b", b"2")
    assert sorted(bridge.nvs.keys()) == ["a", "b"]
    bridge.nvs.clear()
    assert bridge.nvs.keys() == []


def test_key_length_validated(bridge):
    with pytest.raises(ValueError):
        bridge.nvs.set("k" * 16, b"v")
    with pytest.raises(ValueError):
        bridge.nvs.get("")
