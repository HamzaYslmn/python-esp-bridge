"""Multiple-device support: names, MAC/name selection, connect_all."""
import pytest

import espbridge.bridge as bridge_mod
from espbridge.bridge import Bridge, Info, connect_all
from espbridge.errors import NoDeviceError
from espbridge.transport import PortInfo
from fake_firmware import FakeFirmware


def test_info_parses_name_tail():
    fw = FakeFirmware(name="relays")
    info = Info.parse(fw._info())
    assert info.name == "relays"


def test_info_without_name_tail_is_backward_compatible():
    fw = FakeFirmware()
    info = Info.parse(fw._info()[:18])  # firmware predating the name field
    assert info.name == ""


def test_set_name_persists_and_updates_info(bridge, fw):
    assert bridge.info.name == ""
    bridge.set_name("relays")
    assert fw.name == "relays"
    assert bridge.info.name == "relays"
    with pytest.raises(ValueError):
        bridge.set_name("x" * 33)


def _fake_farm(monkeypatch, fakes: dict[str, FakeFirmware]):
    """Patch port discovery/opening so each fake looks like a serial port."""
    monkeypatch.setattr(bridge_mod, "find_ports",
                        lambda: [PortInfo(d, "cp210x", "") for d in fakes])

    def fake_serial(port, baud=115200, usb_chip=None):
        fakes[port].boot()
        return fakes[port].transport

    monkeypatch.setattr(bridge_mod, "SerialTransport", fake_serial)


def test_select_by_name_probes_ports(monkeypatch):
    fakes = {
        "COM7": FakeFirmware(name="sensors", mac="aabbccddee01"),
        "COM8": FakeFirmware(name="relays", mac="aabbccddee02"),
    }
    _fake_farm(monkeypatch, fakes)
    b = Bridge(name="relays", upgrade_baud=False, reset_on_open=False)
    try:
        assert b.info.name == "relays"
        assert b.info.mac == "aa:bb:cc:dd:ee:02"
    finally:
        b.close()


def test_select_by_mac(monkeypatch):
    fakes = {
        "COM7": FakeFirmware(name="sensors", mac="aabbccddee01"),
        "COM8": FakeFirmware(name="relays", mac="aabbccddee02"),
    }
    _fake_farm(monkeypatch, fakes)
    b = Bridge(mac="AA:BB:CC:DD:EE:01", upgrade_baud=False, reset_on_open=False)
    try:
        assert b.info.name == "sensors"
    finally:
        b.close()


def test_no_selector_with_multiple_ports_raises(monkeypatch):
    fakes = {"COM7": FakeFirmware(), "COM8": FakeFirmware()}
    _fake_farm(monkeypatch, fakes)
    with pytest.raises(NoDeviceError, match="multiple ESP32-like ports"):
        Bridge(upgrade_baud=False, reset_on_open=False)


def test_selector_no_match_lists_candidates(monkeypatch):
    fakes = {"COM7": FakeFirmware(name="sensors", mac="aabbccddee01")}
    _fake_farm(monkeypatch, fakes)
    with pytest.raises(NoDeviceError, match="sensors"):
        Bridge(name="nonexistent", upgrade_baud=False, reset_on_open=False)


def test_connect_all_and_helpers(monkeypatch):
    fakes = {
        "COM7": FakeFirmware(name="sensors", mac="aabbccddee01"),
        "COM8": FakeFirmware(name="relays", mac="aabbccddee02"),
    }
    _fake_farm(monkeypatch, fakes)
    with connect_all(upgrade_baud=False, reset_on_open=False) as boards:
        assert len(boards) == 2
        assert boards.by_name("relays").info.mac == "aa:bb:cc:dd:ee:02"
        assert boards.by_mac("aabbccddee01").info.name == "sensors"
        with pytest.raises(NoDeviceError):
            boards.by_name("nope")
        # Independent boards: drive different pins on each
        boards.by_name("relays").gpio.mode(2, "output")
        boards.by_name("relays").gpio.write(2, 1)
        assert fakes["COM8"].gpio_levels[2] == 1
        assert 2 not in fakes["COM7"].gpio_levels
    assert all(b._closing for b in boards)
