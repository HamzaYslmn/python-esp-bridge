"""Multiple-device support: identity selection, plural Bridge(), BridgeSet."""
import pytest

import espbridge.bridge as bridge_mod
from espbridge import constants as C
from espbridge.bridge import Bridge, Info
from espbridge.errors import NoDeviceError
from espbridge.transports import PortInfo
from fake_firmware import FakeFirmware


def test_info_parses_name_tail():
    info = Info.parse(FakeFirmware(name="relays")._info())
    assert info.name == "relays"
    assert info.ident == "relays"


def test_info_without_a_name_identifies_by_mac():
    info = Info.parse(FakeFirmware(mac="aabbccddee01")._info())
    assert info.name == ""
    assert info.ident == "aa:bb:cc:dd:ee:01"


def test_set_name_persists_and_updates_info(bridge, fw):
    assert bridge.info.name == ""
    bridge.set_name("relays")
    assert fw.name == "relays"
    assert bridge.info.name == "relays"


def test_set_name_refuses_a_name_too_long_for_the_advert(bridge):
    """The cap keeps "espbridge_<name>" inside the BLE scan response, so a name
    that would be truncated over the air is refused outright."""
    bridge.set_name("a" * C.BRIDGE_NAME_MAX)
    with pytest.raises(ValueError, match=f"{C.BRIDGE_NAME_MAX} bytes"):
        bridge.set_name("a" * (C.BRIDGE_NAME_MAX + 1))


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
    monkeypatch.setattr(bridge_mod, "_ble_candidates", lambda *a, **k: [])


def test_select_by_name_probes_ports(monkeypatch):
    fakes = {
        "COM7": FakeFirmware(name="sensors", mac="aabbccddee01"),
        "COM8": FakeFirmware(name="relays", mac="aabbccddee02"),
    }
    _fake_farm(monkeypatch, fakes)
    b = Bridge("relays", upgrade_baud=False, reset_on_open=False)
    try:
        assert b.info.name == "relays"
        assert b.info.mac == "aa:bb:cc:dd:ee:02"
    finally:
        b.close()


def test_the_same_argument_also_takes_a_mac(monkeypatch):
    fakes = {
        "COM7": FakeFirmware(name="sensors", mac="aabbccddee01"),
        "COM8": FakeFirmware(name="relays", mac="aabbccddee02"),
    }
    _fake_farm(monkeypatch, fakes)
    for target in ("AA:BB:CC:DD:EE:01", "aabbccddee01", "aa-bb-cc-dd-ee-01"):
        b = Bridge(target, upgrade_baud=False, reset_on_open=False)
        try:
            assert b.info.name == "sensors", target
        finally:
            b.close()


def test_a_mac_ending_in_digits_is_never_read_as_host_and_port(monkeypatch):
    """The selector is an identity, full stop — no shape-guessing.

    'c0:49:ef:d0:3f:30' rpartitions into a plausible host + port 30, which an
    earlier version dialled over TCP instead of matching the board.
    """
    _fake_farm(monkeypatch, {"COM7": FakeFirmware(mac="c049efd03f30")})
    monkeypatch.setattr(bridge_mod, "TcpTransport",
                        lambda *a, **k: pytest.fail("must not open a TCP link"))
    b = Bridge("c0:49:ef:d0:3f:30", upgrade_baud=False, reset_on_open=False)
    try:
        assert b.info.mac == "c0:49:ef:d0:3f:30"
    finally:
        b.close()


def test_no_selector_connects_to_every_port(monkeypatch):
    """No selector means every board — never a silent pick of one of several.

    Auto-selecting is how a suite once passed 10/10 against a board that was not
    under test, so an unqualified peripheral call on a multi-board set raises and
    says how to choose.
    """
    fakes = {"COM7": FakeFirmware(mac="aabbccddee01", name="sensors"),
             "COM8": FakeFirmware(mac="aabbccddee02", name="relays")}
    _fake_farm(monkeypatch, fakes)
    with Bridge(upgrade_baud=False, reset_on_open=False) as boards:
        assert boards.idents() == ["sensors", "relays"]
        with pytest.raises(NoDeviceError, match="ambiguous"):
            boards.gpio.write(2, 1)
        boards["relays"].gpio.mode(2, "output")
        assert fakes["COM8"].gpio_modes[2] == 1
        assert 2 not in fakes["COM7"].gpio_modes


def test_a_list_connects_to_exactly_those_boards(monkeypatch):
    fakes = {"COM7": FakeFirmware(mac="aabbccddee01", name="sensors"),
             "COM8": FakeFirmware(mac="aabbccddee02", name="relays"),
             "COM9": FakeFirmware(mac="aabbccddee03")}
    _fake_farm(monkeypatch, fakes)
    # Mixed spellings in one list: a name and a MAC.
    with Bridge(["relays", "aabbccddee03"],
                upgrade_baud=False, reset_on_open=False) as boards:
        assert sorted(boards.idents()) == ["aa:bb:cc:dd:ee:03", "relays"]


def test_a_partly_found_list_raises_naming_what_is_missing(monkeypatch):
    """Driving 2 of the 3 boards you asked for is the auto-select bug again."""
    _fake_farm(monkeypatch, {"COM7": FakeFirmware(mac="aabbccddee01")})
    with pytest.raises(NoDeviceError, match="nonexistent"):
        Bridge(["aabbccddee01", "nonexistent"],
               upgrade_baud=False, reset_on_open=False)


def test_one_board_set_reads_like_one_bridge(monkeypatch):
    """The 90% case: one board attached, so Bridge() behaves as it always did."""
    fakes = {"COM7": FakeFirmware(mac="aabbccddee01", name="solo")}
    _fake_farm(monkeypatch, fakes)
    with Bridge(upgrade_baud=False, reset_on_open=False) as esp:
        assert esp.info.name == "solo"          # forwarded to the only member
        esp.gpio.mode(2, "output")
        esp.gpio.write(2, 1)
        assert fakes["COM7"].gpio_levels[2] == 1
        assert esp.ping() >= 0


def test_set_never_forwards_private_or_dunder_lookups(monkeypatch):
    _fake_farm(monkeypatch, {"COM7": FakeFirmware(mac="aabbccddee01")})
    with Bridge(upgrade_baud=False, reset_on_open=False) as esp:
        for attr in ("_secret", "__deepcopy__", "_ipython_canary_"):
            with pytest.raises(AttributeError):
                getattr(esp, attr)


def test_selector_returns_a_single_bridge(monkeypatch):
    """Arity follows the selector: one name is one board, no selector is all."""
    fakes = {"COM7": FakeFirmware(mac="aabbccddee01", name="solo"),
             "COM8": FakeFirmware(mac="aabbccddee02", name="other")}
    _fake_farm(monkeypatch, fakes)
    esp = Bridge("solo", upgrade_baud=False, reset_on_open=False)
    try:
        assert isinstance(esp, Bridge)
        assert esp.info.name == "solo"
    finally:
        esp.close()
    plural = Bridge(upgrade_baud=False, reset_on_open=False)
    try:
        assert not isinstance(plural, Bridge)
        assert isinstance(plural, bridge_mod.BridgeSet)
    finally:
        plural.close()


def test_default_lists_ble_before_serial(monkeypatch):
    # No selector means every board, so both links show up — but BLE is still
    # enumerated first, which is the order a single-board selector probes in.
    ble = FakeFirmware(mac="aabbccddee0b")
    ble.boot()
    serial = FakeFirmware(mac="aabbccddee0c")
    monkeypatch.setattr(bridge_mod, "find_ports",
                        lambda: [PortInfo("COM7", "cp210x", "")])

    def fake_serial(port, baud=115200, usb_chip=None):
        serial.boot()
        return serial.transport

    monkeypatch.setattr(bridge_mod, "SerialTransport", fake_serial)
    monkeypatch.setattr(bridge_mod, "_ble_candidates",
                        lambda *a, **k: [(lambda: ble.transport, "BLE test", None)])
    with Bridge(upgrade_baud=False, reset_on_open=False) as boards:
        assert boards.idents() == ["aa:bb:cc:dd:ee:0b", "aa:bb:cc:dd:ee:0c"]


def test_falls_back_to_serial_when_no_ble(monkeypatch):
    # _fake_farm patches out BLE discovery, so Bridge has to use the USB serial board.
    _fake_farm(monkeypatch, {"COM7": FakeFirmware(mac="aabbccddee0c")})
    b = Bridge("aabbccddee0c", upgrade_baud=False, reset_on_open=False)
    try:
        assert b.info.mac == "aa:bb:cc:dd:ee:0c"
    finally:
        b.close()


def test_no_device_over_ble_or_serial_errors(monkeypatch):
    monkeypatch.setattr(bridge_mod, "find_ports", lambda: [])
    monkeypatch.setattr(bridge_mod, "_ble_candidates", lambda *a, **k: [])
    with pytest.raises(NoDeviceError, match="no bridge found"):
        Bridge("relays", upgrade_baud=False, reset_on_open=False)


def _boom_ble(*a, **k):
    raise AssertionError("ble=False must not attempt Bluetooth")


def _no_ble(*a, **k):
    raise NoDeviceError("no Bluetooth devices in range")


def test_ble_false_disables_bluetooth_and_uses_serial(monkeypatch):
    # ble=False must skip Bluetooth entirely (not even probe it) and use USB.
    fakes = {"COM7": FakeFirmware(mac="aabbccddee0d", name="usb-only")}
    monkeypatch.setattr(bridge_mod, "find_ports",
                        lambda: [PortInfo(d, "cp210x", "") for d in fakes])

    def fake_serial(port, baud=115200, usb_chip=None):
        fakes[port].boot()
        return fakes[port].transport

    monkeypatch.setattr(bridge_mod, "SerialTransport", fake_serial)
    monkeypatch.setattr(bridge_mod, "_ble_candidates", _boom_ble)
    b = Bridge("usb-only", ble=False, upgrade_baud=False, reset_on_open=False)
    try:
        assert b.info.mac == "aa:bb:cc:dd:ee:0d"
    finally:
        b.close()
    Bridge(ble=False, upgrade_baud=False, reset_on_open=False).close()


def test_ble_true_never_touches_a_serial_port(monkeypatch):
    # A serial board is present, but ble=True pins Bluetooth: with nothing
    # advertising it must raise rather than quietly connecting over USB.
    monkeypatch.setattr(bridge_mod, "find_ports",
                        lambda: [PortInfo("COM7", "cp210x", "")])
    monkeypatch.setattr(bridge_mod, "SerialTransport",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("ble=True must not use serial")))
    monkeypatch.setattr(bridge_mod, "_ble_candidates", _no_ble)
    with pytest.raises(NoDeviceError):
        Bridge(ble=True, upgrade_baud=False, reset_on_open=False)
    with pytest.raises(NoDeviceError):
        Bridge("relays", ble=True, upgrade_baud=False, reset_on_open=False)


def test_selector_no_match_lists_candidates(monkeypatch):
    _fake_farm(monkeypatch, {"COM7": FakeFirmware(name="sensors",
                                                  mac="aabbccddee01")})
    with pytest.raises(NoDeviceError, match="sensors"):
        Bridge("nonexistent", upgrade_baud=False, reset_on_open=False)


def test_plural_bridge_and_set_helpers(monkeypatch):
    fakes = {
        "COM7": FakeFirmware(name="sensors", mac="aabbccddee01"),
        "COM8": FakeFirmware(name="relays", mac="aabbccddee02"),
    }
    _fake_farm(monkeypatch, fakes)
    with Bridge(upgrade_baud=False, reset_on_open=False) as boards:
        assert len(boards) == 2
        assert boards["relays"] is boards[1]
        assert boards["AA:BB:CC:DD:EE:02"] is boards[1]   # name or MAC, one index
        assert boards["sensors"].info.mac == "aa:bb:cc:dd:ee:01"
        with pytest.raises(NoDeviceError):
            boards["nope"]
        # Commands on one board must not affect the other board's state.
        boards["relays"].gpio.mode(2, "output")
        boards["relays"].gpio.write(2, 1)
        assert fakes["COM8"].gpio_levels[2] == 1    # pin 2 on "relays" (COM8) was set
        assert 2 not in fakes["COM7"].gpio_levels   # "sensors" (COM7) untouched
    assert all(b._closing for b in boards)
