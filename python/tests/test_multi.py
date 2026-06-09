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
    info = Info.parse(fw._info()[:18])  # truncated to 18 bytes: simulates older firmware without the name field
    assert info.name == ""


def test_set_name_persists_and_updates_info(bridge, fw):
    assert bridge.info.name == ""
    bridge.set_name("relays")
    assert fw.name == "relays"
    assert bridge.info.name == "relays"
    with pytest.raises(ValueError):
        bridge.set_name("x" * 33)


def _fake_farm(monkeypatch, fakes: dict[str, FakeFirmware]):
    """Patch port discovery and opening so each FakeFirmware looks like a real serial port.

    Also neutralizes BLE discovery: Bridge tries BLE before serial by default,
    which would make serial-path tests non-deterministic and require hardware.
    """
    monkeypatch.setattr(bridge_mod, "find_ports",
                        lambda: [PortInfo(d, "cp210x", "") for d in fakes])

    def fake_serial(port, baud=115200, usb_chip=None):
        fakes[port].boot()
        return fakes[port].transport

    monkeypatch.setattr(bridge_mod, "SerialTransport", fake_serial)
    monkeypatch.setattr(bridge_mod.Bridge, "_ble_candidates",
                        staticmethod(lambda *a, **k: []))


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


def test_no_selector_with_multiple_ports_probes_and_picks_first(monkeypatch):
    # When no name/MAC selector is given and multiple ports are present, Bridge
    # probes them in order and connects to the first one that responds.
    # (Since v0.1.1 a log line also tells the user how to pin to a specific device.)
    fakes = {"COM7": FakeFirmware(mac="aabbccddee01"),
             "COM8": FakeFirmware(mac="aabbccddee02")}
    _fake_farm(monkeypatch, fakes)
    b = Bridge(upgrade_baud=False, reset_on_open=False)
    try:
        assert b.info.mac == "aa:bb:cc:dd:ee:01"
    finally:
        b.close()


def test_default_prefers_ble_over_serial(monkeypatch):
    # When no transport is specified, Bridge tries BLE candidates before falling
    # back to USB serial.  The BLE device should win here.
    ble = FakeFirmware(mac="aabbccddee0b")
    ble.boot()
    serial = FakeFirmware(mac="aabbccddee0c")
    monkeypatch.setattr(bridge_mod, "find_ports",
                        lambda: [PortInfo("COM7", "cp210x", "")])

    def fake_serial(port, baud=115200, usb_chip=None):
        serial.boot()
        return serial.transport

    monkeypatch.setattr(bridge_mod, "SerialTransport", fake_serial)
    monkeypatch.setattr(bridge_mod.Bridge, "_ble_candidates",
                        staticmethod(lambda *a, **k: [(lambda: ble.transport,
                                                       "BLE test", None)]))
    b = Bridge(upgrade_baud=False, reset_on_open=False)
    try:
        assert b.info.mac == "aa:bb:cc:dd:ee:0b"  # BLE candidate was chosen over the serial one
    finally:
        b.close()


def test_falls_back_to_serial_when_no_ble(monkeypatch):
    # _fake_farm patches out BLE discovery, so Bridge has to use the USB serial board.
    _fake_farm(monkeypatch, {"COM7": FakeFirmware(mac="aabbccddee0c")})
    b = Bridge(upgrade_baud=False, reset_on_open=False)
    try:
        assert b.info.mac == "aa:bb:cc:dd:ee:0c"
    finally:
        b.close()


def test_no_device_over_ble_or_serial_errors(monkeypatch):
    monkeypatch.setattr(bridge_mod, "find_ports", lambda: [])
    monkeypatch.setattr(bridge_mod.Bridge, "_ble_candidates",
                        staticmethod(lambda *a, **k: []))
    with pytest.raises(NoDeviceError, match="no bridge found"):
        Bridge(upgrade_baud=False, reset_on_open=False)


def _boom_ble(*a, **k):
    raise AssertionError("ble=False must not attempt Bluetooth")


def _no_ble(*a, **k):
    raise NoDeviceError("no Bluetooth devices in range")


def test_ble_false_disables_bluetooth_and_uses_serial(monkeypatch):
    # ble=False must skip Bluetooth entirely (not even probe it) and use USB.
    fakes = {"COM7": FakeFirmware(mac="aabbccddee0d")}
    monkeypatch.setattr(bridge_mod, "find_ports",
                        lambda: [PortInfo(d, "cp210x", "") for d in fakes])

    def fake_serial(port, baud=115200, usb_chip=None):
        fakes[port].boot()
        return fakes[port].transport

    monkeypatch.setattr(bridge_mod, "SerialTransport", fake_serial)
    monkeypatch.setattr(bridge_mod.Bridge, "_ble_candidates", staticmethod(_boom_ble))
    b = Bridge(ble=False, upgrade_baud=False, reset_on_open=False)
    try:
        assert b.info.mac == "aa:bb:cc:dd:ee:0d"
    finally:
        b.close()


def test_ble_named_target_does_not_fall_back_to_serial(monkeypatch):
    # A serial board is present, but ble="missing" wants that BLE device only —
    # it must raise rather than silently connecting over USB.
    monkeypatch.setattr(bridge_mod, "find_ports",
                        lambda: [PortInfo("COM7", "cp210x", "")])
    monkeypatch.setattr(bridge_mod, "SerialTransport",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("named BLE target must not use serial")))
    monkeypatch.setattr(bridge_mod.Bridge, "_ble_candidates", staticmethod(_no_ble))
    with pytest.raises(NoDeviceError):
        Bridge(ble="missing", upgrade_baud=False, reset_on_open=False)


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
        # Commands on one board must not affect the other board's state.
        boards.by_name("relays").gpio.mode(2, "output")
        boards.by_name("relays").gpio.write(2, 1)
        assert fakes["COM8"].gpio_levels[2] == 1    # pin 2 on "relays" (COM8) was set
        assert 2 not in fakes["COM7"].gpio_levels   # "sensors" (COM7) must be untouched
    assert all(b._closing for b in boards)
