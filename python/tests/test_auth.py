"""Wireless-link password auth (SYS_AUTH / ST_DENIED)."""
import pytest

from espbridge import AuthError, Bridge
from espbridge import constants as C
from espbridge.protocol import FrameSplitter, decode_frame, encode_frame

from fake_firmware import FakeFirmware


def _drain_frames(fw):
    """Decode every frame the fake firmware has queued for the host."""
    splitter = FrameSplitter()
    frames = []
    while True:
        data = fw.transport.read()
        if not data:
            break
        for chunk in splitter.feed(data):
            frames.append(decode_frame(chunk))
    return frames


def test_ble_auth_ok():
    fw = FakeFirmware(password="espbridge", name="relays")
    # No boot(): like the real BLE link, READY only follows successful auth.
    with Bridge(transport=fw.transport, password="espbridge",
                reset_on_open=False) as esp:
        assert esp.info is not None
        assert esp.info.name == "relays"
        esp.ping()


def test_ble_wrong_password():
    fw = FakeFirmware(password="espbridge")
    with pytest.raises(AuthError):
        Bridge(transport=fw.transport, password="wrong", reset_on_open=False)
    assert fw.transport.closed


def test_ble_default_password_used_when_omitted():
    fw = FakeFirmware(password=C.DEFAULT_PASSWORD)
    with Bridge(transport=fw.transport, reset_on_open=False) as esp:
        esp.ping()


def test_commands_denied_before_auth():
    fw = FakeFirmware(password="espbridge")
    # Bypass Bridge: write a GPIO request straight to the link, pre-auth.
    fw._on_host_bytes(encode_frame(0, 1, C.GPIO_READ, bytes([2])))
    frames = _drain_frames(fw)
    assert len(frames) == 1
    assert frames[0].is_error
    assert frames[0].payload[0] == C.Status.DENIED


def test_usb_path_needs_no_password():
    fw = FakeFirmware()  # password=None = USB semantics
    fw.boot()
    with Bridge(transport=fw.transport, upgrade_baud=False,
                reset_on_open=False) as esp:
        esp.ping()
        # SYS_AUTH is accepted over USB regardless of payload.
        esp.request(C.SYS_AUTH, b"anything")


def test_ble_skips_baud_upgrade():
    fw = FakeFirmware(password="espbridge")
    with Bridge(transport=fw.transport, password="espbridge",
                reset_on_open=False, upgrade_baud=True):
        pass
    assert fw.baud_requests == []  # has_baud=False suppressed SYS_SET_BAUD
