"""Multiple-device support: identity selection, Bridge.all(), BridgeSet."""
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

    def fake_serial(port, baud=115200, usb_chip=None, *, reset=True):
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


def test_no_selector_takes_one_board_and_leaves_the_rest_alone(monkeypatch):
    """Bridge() is one board: the first that answers, not all of them.

    The others must stay untouched — opening every board to use one is what made
    a bare Bridge() cost a connect per radio in range.
    """
    fakes = {"COM7": FakeFirmware(mac="aabbccddee01", name="sensors"),
             "COM8": FakeFirmware(mac="aabbccddee02", name="relays")}
    _fake_farm(monkeypatch, fakes)
    with Bridge(upgrade_baud=False, reset_on_open=False) as esp:
        assert isinstance(esp, Bridge)
        assert esp.info.name == "sensors"       # COM7, the first candidate
        esp.gpio.mode(2, "output")
        esp.gpio.write(2, 1)
        assert fakes["COM7"].gpio_levels[2] == 1
        assert 2 not in fakes["COM8"].gpio_modes
        assert esp.ping() >= 0


def test_all_connects_to_every_port(monkeypatch):
    fakes = {"COM7": FakeFirmware(mac="aabbccddee01", name="sensors"),
             "COM8": FakeFirmware(mac="aabbccddee02", name="relays")}
    _fake_farm(monkeypatch, fakes)
    with Bridge.all(upgrade_baud=False, reset_on_open=False) as boards:
        assert isinstance(boards, bridge_mod.BridgeSet)
        assert boards.idents() == ["sensors", "relays"]
        boards["relays"].gpio.mode(2, "output")
        assert fakes["COM8"].gpio_modes[2] == 1
        assert 2 not in fakes["COM7"].gpio_modes


def test_a_set_is_not_a_board(monkeypatch):
    """No single-board sugar: a set says which boards it holds and how to pick."""
    fakes = {"COM7": FakeFirmware(mac="aabbccddee01", name="sensors"),
             "COM8": FakeFirmware(mac="aabbccddee02", name="relays")}
    _fake_farm(monkeypatch, fakes)
    with Bridge.all(upgrade_baud=False, reset_on_open=False) as boards:
        with pytest.raises(NoDeviceError, match="sensors, relays"):
            boards.gpio.write(2, 1)


def test_all_still_reports_why_when_nothing_connects(monkeypatch):
    """Quiet about a redundant link, loud when the set comes back empty.

    Per-candidate failures are debug noise while other boards are connecting,
    but if none of them works the reasons are the whole answer.
    """
    monkeypatch.setattr(bridge_mod, "find_ports",
                        lambda: [PortInfo("COM7", "cp210x", "")])
    monkeypatch.setattr(bridge_mod, "SerialTransport",
                        lambda *a, **k: (_ for _ in ()).throw(
                            NoDeviceError("port is held by another program")))
    monkeypatch.setattr(bridge_mod, "_ble_candidates", lambda *a, **k: [])
    with pytest.raises(NoDeviceError, match="held by another program"):
        Bridge.all(upgrade_baud=False, reset_on_open=False)


def test_a_list_connects_to_exactly_those_boards(monkeypatch):
    fakes = {"COM7": FakeFirmware(mac="aabbccddee01", name="sensors"),
             "COM8": FakeFirmware(mac="aabbccddee02", name="relays"),
             "COM9": FakeFirmware(mac="aabbccddee03")}
    _fake_farm(monkeypatch, fakes)
    # Mixed spellings in one list: a name and a MAC.
    with Bridge.all(["relays", "aabbccddee03"],
                    upgrade_baud=False, reset_on_open=False) as boards:
        assert sorted(boards.idents()) == ["aa:bb:cc:dd:ee:03", "relays"]


def test_a_list_of_boards_must_be_spelled_bridge_all(monkeypatch):
    _fake_farm(monkeypatch, {"COM7": FakeFirmware(mac="aabbccddee01")})
    with pytest.raises(TypeError, match=r"Bridge\.all"):
        Bridge(["aabbccddee01"], upgrade_baud=False, reset_on_open=False)


def test_a_partly_found_list_raises_naming_what_is_missing(monkeypatch):
    """Driving 2 of the 3 boards you asked for is the auto-select bug again."""
    _fake_farm(monkeypatch, {"COM7": FakeFirmware(mac="aabbccddee01")})
    with pytest.raises(NoDeviceError, match="nonexistent"):
        Bridge.all(["aabbccddee01", "nonexistent"],
                   upgrade_baud=False, reset_on_open=False)


def test_set_never_forwards_private_or_dunder_lookups(monkeypatch):
    _fake_farm(monkeypatch, {"COM7": FakeFirmware(mac="aabbccddee01")})
    with Bridge.all(upgrade_baud=False, reset_on_open=False) as boards:
        for attr in ("_secret", "__deepcopy__", "_ipython_canary_"):
            with pytest.raises(AttributeError):
                getattr(boards, attr)


def test_default_takes_usb_before_bluetooth(monkeypatch):
    """Best link first: a cable beats a radio when both are there.

    Bridge.all() still opens both boards — they are different boards — but the
    order decides which link a single Bridge() lands on.
    """
    ble = FakeFirmware(mac="aabbccddee0b")
    ble.boot()
    serial = FakeFirmware(mac="aabbccddee0c")
    monkeypatch.setattr(bridge_mod, "find_ports",
                        lambda: [PortInfo("COM7", "cp210x", "")])

    def fake_serial(port, baud=115200, usb_chip=None, *, reset=True):
        serial.boot()
        return serial.transport

    monkeypatch.setattr(bridge_mod, "SerialTransport", fake_serial)
    monkeypatch.setattr(bridge_mod, "_ble_candidates",
                        lambda *a, **k: [(lambda: ble.transport, "BLE test",
                                          None, "aa:bb:cc:dd:ee:0b")])
    with Bridge.all(upgrade_baud=False, reset_on_open=False) as boards:
        assert boards.idents() == ["aa:bb:cc:dd:ee:0c", "aa:bb:cc:dd:ee:0b"]
    with Bridge(upgrade_baud=False, reset_on_open=False) as esp:
        assert esp.info.mac == "aa:bb:cc:dd:ee:0c"   # the cable, and only it


def test_a_working_usb_board_never_starts_a_bluetooth_scan(monkeypatch):
    """The cable answered, so the radio is never touched.

    Scanning is the expensive half of connecting — up to 15 s with the adapter
    off — and a plain Bridge() that lands on the first serial port has no reason
    to pay for it. Bridge.all() still does, because it wants every board.
    """
    scans = []

    def counted_ble(*a, **k):
        scans.append(1)
        return []

    _fake_farm(monkeypatch, {"COM7": FakeFirmware(mac="aabbccddee01",
                                                  name="onusb")})
    monkeypatch.setattr(bridge_mod, "_ble_candidates", counted_ble)
    with Bridge(upgrade_baud=False, reset_on_open=False) as esp:
        assert esp.info.name == "onusb"
    assert scans == []                  # never asked

    with Bridge.all(upgrade_baud=False, reset_on_open=False) as boards:
        assert boards.idents() == ["onusb"]
    assert len(scans) == 1              # the fleet wants everything, so it asks


def test_a_dead_usb_port_still_falls_through_to_bluetooth(monkeypatch):
    """Lazy, not blinkered: the next link is enumerated the moment it's needed."""
    ble = FakeFirmware(mac="aabbccddee0b", name="onble")
    ble.boot()
    monkeypatch.setattr(bridge_mod, "find_ports",
                        lambda: [PortInfo("COM7", "cp210x", "")])
    monkeypatch.setattr(bridge_mod, "SerialTransport",
                        lambda *a, **k: (_ for _ in ()).throw(
                            NoDeviceError("port busy")))
    monkeypatch.setattr(bridge_mod, "_ble_candidates",
                        lambda *a, **k: [(lambda: ble.transport, "ble onble",
                                          None, "onble")])
    with Bridge(upgrade_baud=False, reset_on_open=False) as esp:
        assert esp.info.name == "onble"


def test_all_skips_a_board_already_open_on_a_better_link(monkeypatch):
    """One board on two links costs one connect, not two.

    Bluetooth names the board in its advertisement, so the redundant link is
    recognised before it is opened — no wasted connect, and no warning about a
    link that was never needed.
    """
    board = FakeFirmware(mac="aabbccddee0c", name="solo")
    monkeypatch.setattr(bridge_mod, "find_ports",
                        lambda: [PortInfo("COM7", "cp210x", "")])

    def fake_serial(port, baud=115200, usb_chip=None, *, reset=True):
        board.boot()
        return board.transport

    def boom():
        raise AssertionError("must not open the BLE link for a board on USB")

    monkeypatch.setattr(bridge_mod, "SerialTransport", fake_serial)
    monkeypatch.setattr(bridge_mod, "_ble_candidates",
                        lambda *a, **k: [(boom, "ble solo", None, "solo")])
    with Bridge.all(upgrade_baud=False, reset_on_open=False) as boards:
        assert boards.idents() == ["solo"]


def test_falls_back_to_wifi_only_when_usb_and_bluetooth_find_nothing(monkeypatch):
    """The LAN is the last resort: it costs a broadcast, so it is only asked
    when there is no cable and nothing in radio range — and never when a link
    was pinned, which would be a silent fallback to a link nobody asked for."""
    board = FakeFirmware(mac="aabbccddee0e", name="onwifi")
    asked = []

    def fake_wifi(want, tcp_port):
        asked.append(want)
        return [(lambda: board.transport, "wifi 10.0.0.5:3232", None, "onwifi")]

    monkeypatch.setattr(bridge_mod, "find_ports", lambda: [])
    monkeypatch.setattr(bridge_mod, "_ble_candidates", lambda *a, **k: [])
    monkeypatch.setattr(bridge_mod, "_wifi_candidates", fake_wifi)
    board.boot()
    with Bridge(upgrade_baud=False, reset_on_open=False) as esp:
        assert esp.info.name == "onwifi"
    assert len(asked) == 1

    # ble=True pins Bluetooth, so an empty scan is the answer, not a Wi-Fi hunt.
    monkeypatch.setattr(bridge_mod, "_ble_candidates", _no_ble)
    with pytest.raises(NoDeviceError):
        Bridge(ble=True, upgrade_baud=False, reset_on_open=False)
    assert len(asked) == 1


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

    def fake_serial(port, baud=115200, usb_chip=None, *, reset=True):
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
    with Bridge.all(upgrade_baud=False, reset_on_open=False) as boards:
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
