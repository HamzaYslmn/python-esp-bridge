"""ESP-NOW semantics over the fake firmware."""
import threading
import time

import pytest

import fake_firmware
from espbridge import constants as C
from espbridge.bridge import Bridge
from espbridge.errors import BridgeTimeoutError, RemoteError, UnsupportedError
from espbridge.espnow import BROADCAST, mac_to_bytes, mac_to_str
from fake_firmware import FakeFirmware

PEER = "a0:b1:c2:d3:e4:f5"
PEER6 = bytes.fromhex("a0b1c2d3e4f5")


# ---- MAC helpers ----------------------------------------------------------------

def test_mac_to_bytes_accepts_common_formats():
    assert mac_to_bytes("a0:b1:c2:d3:e4:f5") == PEER6
    assert mac_to_bytes("A0-B1-C2-D3-E4-F5") == PEER6
    assert mac_to_bytes("a0b1c2d3e4f5") == PEER6
    assert mac_to_bytes(PEER6) == PEER6


def test_mac_round_trip():
    assert mac_to_bytes(mac_to_str(PEER6)) == PEER6


@pytest.mark.parametrize("bad", ["a0:b1", "zz:zz:zz:zz:zz:zz", b"\x01\x02"])
def test_mac_to_bytes_rejects_garbage(bad):
    with pytest.raises(ValueError):
        mac_to_bytes(bad)


# ---- lifecycle ---------------------------------------------------------------------

def test_begin_returns_own_mac(bridge, fw):
    assert bridge.espnow.begin() == "24:a1:60:12:34:56"
    assert fw.espnow_inited


def test_begin_rejects_bad_channel(bridge):
    with pytest.raises(ValueError):
        bridge.espnow.begin(channel=15)


def test_end_deinits(bridge, fw):
    bridge.espnow.begin()
    bridge.espnow.end()
    assert not fw.espnow_inited


def test_set_pmk_length_validated_host_side(bridge, fw):
    bridge.espnow.begin()
    with pytest.raises(ValueError):
        bridge.espnow.set_pmk(b"short")
    bridge.espnow.set_pmk(b"\x01" * 16)
    assert fw.espnow_pmk == b"\x01" * 16


def test_power_save_packs_window_and_interval(bridge, fw):
    bridge.espnow.begin()
    bridge.espnow.power_save(50, 200)
    assert fw.espnow_ps == (50, 200)
    bridge.espnow.power_save(65535)  # interval omitted -> 0 (keep current)
    assert fw.espnow_ps == (65535, 0)


def test_power_save_validates_ranges(bridge):
    bridge.espnow.begin()
    with pytest.raises(ValueError):
        bridge.espnow.power_save(65536)
    with pytest.raises(ValueError):
        bridge.espnow.power_save(50, 0)


# ---- peers -----------------------------------------------------------------------

def test_add_and_remove_peer(bridge, fw):
    bridge.espnow.begin()
    bridge.espnow.add_peer(PEER)
    assert fw.espnow_peers == {PEER6: None}
    bridge.espnow.remove_peer(PEER)
    assert fw.espnow_peers == {}


def test_add_encrypted_peer_sends_lmk(bridge, fw):
    bridge.espnow.begin()
    lmk = bytes(range(16))
    bridge.espnow.add_peer(PEER, lmk=lmk)
    assert fw.espnow_peers == {PEER6: lmk}


def test_add_peer_rejects_short_lmk(bridge):
    bridge.espnow.begin()
    with pytest.raises(ValueError):
        bridge.espnow.add_peer(PEER, lmk=b"short")


# ---- send -------------------------------------------------------------------------

def test_send_returns_delivery_status(bridge, fw):
    bridge.espnow.begin()
    bridge.espnow.add_peer(PEER)
    assert bridge.espnow.send(PEER, b"hello") is True
    assert fw.espnow_sent == [(PEER6, b"hello")]
    fw.espnow_deliver = False  # simulate the peer not ACKing (e.g. out of range)
    assert bridge.espnow.send(PEER, b"again") is False


def test_send_to_unknown_peer_raises(bridge):
    bridge.espnow.begin()
    with pytest.raises(RemoteError):
        bridge.espnow.send(PEER, b"hi")


def test_send_rejects_oversized_payload(bridge, fw):
    bridge.espnow.begin()
    bridge.espnow.add_peer(PEER)
    with pytest.raises(ValueError):
        bridge.espnow.send(PEER, b"x" * 251)
    assert fw.espnow_sent == []  # oversized payload must be rejected host-side, before anything is sent
    bridge.espnow.send(PEER, b"x" * 250)  # 250 bytes is exactly the ESP-NOW limit and must succeed


def test_send_nowait_fires_send_result_event(bridge, fw):
    bridge.espnow.begin()
    bridge.espnow.add_peer(PEER)
    results = []
    got = threading.Event()
    bridge.espnow.on_send_result(lambda mac, ok: (results.append((mac, ok)), got.set()))
    bridge.espnow.send(PEER, b"fast", wait=False)
    assert got.wait(2.0)
    assert results == [(PEER, True)]
    assert fw.espnow_sent == [(PEER6, b"fast")]


def test_broadcast_registers_bcast_peer_once(bridge, fw):
    bridge.espnow.begin()
    bridge.espnow.broadcast(b"one")
    bridge.espnow.broadcast(b"two")
    assert list(fw.espnow_peers) == [mac_to_bytes(BROADCAST)]
    assert fw.espnow_sent == [(mac_to_bytes(BROADCAST), b"one"),
                              (mac_to_bytes(BROADCAST), b"two")]


# ---- receive -----------------------------------------------------------------------

def test_on_receive_dispatch(bridge, fw):
    bridge.espnow.begin()
    got = threading.Event()
    packets = []
    bridge.espnow.on_receive(lambda mac, data, rssi: (packets.append((mac, data, rssi)),
                                                      got.set()))
    fw.emit_espnow_rx(PEER6, b"ping", rssi=-72)
    assert got.wait(2.0)
    assert packets == [(PEER, b"ping", -72)]


def test_polled_read_preserves_order(bridge, fw):
    bridge.espnow.begin()
    fw.emit_espnow_rx(PEER6, b"first", rssi=-10)
    fw.emit_espnow_rx(PEER6, b"second", rssi=-20)
    assert bridge.espnow.read(timeout=2.0) == (PEER, b"first", -10)
    assert bridge.espnow.read(timeout=2.0) == (PEER, b"second", -20)
    deadline = time.monotonic() + 2.0
    while bridge.espnow.available() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bridge.espnow.available() == 0


def test_read_timeout(bridge):
    bridge.espnow.begin()
    with pytest.raises(BridgeTimeoutError):
        bridge.espnow.read(timeout=0.1)


# ---- capability gate ------------------------------------------------------------------

def test_unsupported_without_cap(monkeypatch):
    monkeypatch.setattr(fake_firmware, "CAPS", C.Cap.WIFI | C.Cap.BLE)
    fake = FakeFirmware()
    fake.boot()
    b = Bridge(transport=fake.transport, upgrade_baud=False, reset_on_open=False)
    try:
        with pytest.raises(UnsupportedError):
            b.espnow.begin()
    finally:
        b.close()
