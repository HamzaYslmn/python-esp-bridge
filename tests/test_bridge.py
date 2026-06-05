"""Bridge core tests over MockTransport + FakeFirmware (no hardware)."""
import threading

import pytest

from espbridge import constants as C
from espbridge.bridge import Bridge
from espbridge.constants import Cap, ChipModel, Status
from espbridge.errors import BridgeTimeoutError, ProtocolError, RemoteError
from fake_firmware import FakeFirmware


def test_handshake_parses_ready_banner(bridge):
    info = bridge.info
    assert info.chip is ChipModel.ESP32
    assert info.protocol == C.PROTOCOL_VERSION
    assert info.fw_version == (0, 0, 1)
    assert info.mac == "24:a1:60:12:34:56"
    assert Cap.DAC in info.caps and Cap.WIFI in info.caps
    assert info.gpio_count == 40


def test_handshake_falls_back_to_info_poll():
    fw = FakeFirmware()  # no boot(): banner never arrives, host polls SYS_INFO
    b = Bridge(transport=fw.transport, upgrade_baud=False, reset_on_open=False)
    try:
        assert b.info is not None and b.info.chip is ChipModel.ESP32
    finally:
        b.close()


def test_protocol_version_mismatch_raises():
    fw = FakeFirmware(proto_version=99)
    fw.boot()
    with pytest.raises(ProtocolError, match="protocol mismatch"):
        Bridge(transport=fw.transport, upgrade_baud=False, reset_on_open=False)


def test_ping_roundtrip(bridge):
    assert bridge.ping(b"x" * 100) >= 0


def test_baud_upgrade_negotiation():
    fw = FakeFirmware()
    fw.boot()
    b = Bridge(transport=fw.transport, upgrade_baud=True, target_baud=921_600,
               reset_on_open=False)
    try:
        assert fw.baud_requests == [921_600]
        assert fw.transport.baud == 921_600
    finally:
        b.close()


def test_gpio_write_read(bridge, fw):
    bridge.gpio.mode(2, "output")
    assert fw.gpio_modes[2] == 1
    bridge.gpio.write(2, 1)
    assert fw.gpio_levels[2] == 1
    assert bridge.gpio.read(2) == 1
    bridge.gpio.write_many({4: 1, 5: 0, 12: 1})
    assert fw.gpio_levels[4] == 1 and fw.gpio_levels[5] == 0 and fw.gpio_levels[12] == 1


def test_remote_error_maps_status(bridge):
    with pytest.raises(RemoteError) as ei:
        bridge.gpio.write(13, 1)  # fake: write before set_mode -> BAD_PIN
    assert ei.value.status is Status.BAD_PIN


def test_unknown_command_raises(bridge):
    with pytest.raises(RemoteError) as ei:
        bridge.request(0x7F01)
    assert ei.value.status is Status.UNKNOWN_CMD


def test_timeout_when_firmware_silent(bridge, fw):
    fw.blackhole_cmds.add(C.SYS_FREE_HEAP)
    with pytest.raises(BridgeTimeoutError):
        bridge.request(C.SYS_FREE_HEAP, timeout=0.2)
    # seq slot must be reclaimed and the link must still work afterwards
    fw.blackhole_cmds.clear()
    assert bridge.free_heap()["free"] == 200_000


def test_gpio_edge_events_dispatch(bridge, fw):
    got = []
    done = threading.Event()

    def on_edge(ev):
        got.append(ev)
        done.set()

    bridge.gpio.watch(4, "falling", debounce_ms=20, callback=on_edge)
    assert fw.watching[4] == (2, 20)
    fw.emit(C.GPIO_EDGE_EVT, bytes([4, 0]) + (12345).to_bytes(4, "big"))
    assert done.wait(2.0)
    assert got[0].pin == 4 and got[0].level == 0 and got[0].millis == 12345


def test_wildcard_event_handler(bridge, fw):
    frames = []
    done = threading.Event()
    bridge.on_event(None, lambda f: (frames.append(f), done.set()))
    fw.emit(C.SYS_LOG, b"\x01hello")
    assert done.wait(2.0)
    assert frames[0].cmd == C.SYS_LOG and frames[0].is_event


def test_concurrent_requests_correlate_by_seq(bridge):
    """Hammer the bridge from several threads; every echo must match its payload."""
    errors = []

    def worker(tag: int):
        try:
            for i in range(25):
                payload = f"{tag}:{i}".encode()
                if bridge.request(C.SYS_PING, payload) != payload:
                    errors.append(f"mismatch for {payload}")
        except Exception as e:  # pragma: no cover
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
