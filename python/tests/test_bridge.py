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
    assert info.fw_version == (0, 3, 0)
    assert info.mac == "24:a1:60:12:34:56"
    assert Cap.DAC in info.caps and Cap.WIFI in info.caps
    assert info.gpio_count == 40


def test_handshake_falls_back_to_info_poll():
    fw = FakeFirmware()  # boot() is not called, so the READY banner never arrives;
                         # the host must fall back to polling SYS_INFO instead
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


def test_gpio_write_returns_readback(bridge, fw):
    bridge.gpio.mode(2, "output")
    assert bridge.gpio.write(2, 1) == 1          # write returns the confirmed level
    assert bridge.gpio.write(2, 0) == 0
    assert bridge.gpio.write(2, 1, verify=True) == 1  # verify passes when it matches


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
    # The timed-out sequence slot must be reclaimed so the bridge can keep
    # sending new commands on the same link without waiting for the dead slot.
    fw.blackhole_cmds.clear()
    assert bridge.free_heap()["free"] == 200_000


def test_cmd_name_lookup():
    assert C.cmd_name(C.I2C_WRITE) == "I2C_WRITE (0x4003)"
    assert C.cmd_name(C.GPIO_WRITE) == "GPIO_WRITE (0x1002)"
    assert C.cmd_name(0x7F01) == "0x7F01"  # completely unknown opcodes fall back to hex notation
    # UART_CHUNK and NET_CHUNK share a numeric prefix with valid command codes but
    # are chunk-size constants, not commands.  They must not be returned as command names.
    assert C.cmd_name(C.UART_CHUNK) == "0x0100"
    assert C.cmd_name(C.NET_CHUNK) == "0x0200"


def test_remote_error_message_names_cmd_and_hints(bridge):
    with pytest.raises(RemoteError, match=r"I2C_WRITE \(0x4003\) failed: IO — no ACK"):
        bridge.i2c.write(0x3C, b"\x00")  # no fake device at 0x3C -> ST_IO


def test_retry_recovers_a_lost_request(bridge, fw):
    fw.drop_once_cmds.add(C.SYS_FREE_HEAP)  # first frame vanishes, retry lands
    assert bridge.request(C.SYS_FREE_HEAP, timeout=0.2)
    assert fw.handled.count(C.SYS_FREE_HEAP) == 2


def test_non_idempotent_commands_are_not_retried(bridge, fw):
    fw.drop_once_cmds.add(C.UART_WRITE)
    with pytest.raises(BridgeTimeoutError, match="link is alive"):
        bridge.request(C.UART_WRITE, b"\x01x", timeout=0.2)
    assert fw.handled.count(C.UART_WRITE) == 1  # never re-sent


def test_timeout_error_diagnoses_dead_link(bridge, fw):
    fw.blackhole_cmds.update({C.GPIO_READ, C.SYS_PING})
    with pytest.raises(BridgeTimeoutError, match="no longer answers"):
        bridge.request(C.GPIO_READ, b"\x02", timeout=0.1)


def test_send_burst_is_fenced(bridge, fw):
    """Pipelined fire-and-forget writes must be throttled to burst_window.

    When many wait=False writes are pipelined the bridge periodically inserts a
    SYS_PING "fence" to prevent the ESP32's receive buffer from overflowing.
    Once a normal (waited) write completes, the unacked counter must reset to zero.
    """
    fw.i2c_devices[0x3C] = b""
    bridge._t.burst_window = 150  # force a tiny window so fences fire quickly (normally much larger)
    for _ in range(10):
        bridge.i2c.write(0x3C, b"\x40" + b"\xff" * 40, wait=False)
    assert len(fw.i2c_writes) == 10            # all writes were delivered despite the throttling
    assert fw.handled.count(C.SYS_PING) >= 2   # at least two SYS_PING fences were inserted
    bridge.i2c.write(0x3C, b"\x00\xaf")        # a synchronous write drains the pipeline
    assert bridge._unacked == 0


def test_free_heap_reports_link_rx_drops(bridge):
    assert bridge.free_heap()["link_rx_dropped"] == 0


def test_cpu_freq_round_trip(bridge, fw):
    assert bridge.cpu_freq(80) == 80
    assert fw.cpu_mhz == 80
    with pytest.raises(ValueError):
        bridge.cpu_freq(120)


def test_link_power_modes(bridge, fw):
    bridge.link_power("battery")
    assert fw.link_power_mode == 1
    bridge.link_power("performance")
    assert fw.link_power_mode == 0
    with pytest.raises(ValueError):
        bridge.link_power("turbo")


def test_power_mode_battery_applies_both(bridge, fw):
    assert bridge.power_mode("battery") == {"cpu_mhz": 80, "ble_link": "battery"}
    assert (fw.cpu_mhz, fw.link_power_mode) == (80, 1)
    assert bridge.power_mode("performance") == {"cpu_mhz": 240, "ble_link": "performance"}


def test_power_mode_skips_ble_on_usb_session(bridge, fw):
    fw.ble_central = False  # firmware replies NOT_INIT to LINK_POWER
    assert bridge.power_mode("battery") == {"cpu_mhz": 80, "ble_link": None}


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
    """Verify that responses are matched to the correct request under concurrency.

    Eight threads each fire 25 SYS_PING commands in parallel.  Because each
    request carries a unique sequence number, the bridge must never mix up a
    reply from one thread with a pending request from another.
    """
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
