"""Hub: one owner Bridge shared by many clients over a local socket.

The owner runs against the in-process FakeFirmware; clients attach over real
TCP sockets via Bridge(share=...), exercising the full relay path end to end.
"""
from __future__ import annotations

import time

import pytest

from espbridge import constants as C
from espbridge import hub as hub_mod
from espbridge.bridge import Bridge
from fake_firmware import FakeFirmware


@pytest.fixture()
def served():
    fake = FakeFirmware()
    fake.boot()
    # Owner over the fake transport, no baud/reset dance; keepalive off for the test.
    hub = hub_mod.serve(":0", keepalive=None, transport=fake.transport,
                        upgrade_baud=False, reset_on_open=False).start()
    host, port = hub.address
    clients: list[Bridge] = []

    def client() -> Bridge:
        b = Bridge(share=f"{host}:{port}")
        clients.append(b)
        return b

    yield fake, hub, client
    for b in clients:
        b.close()
    hub.stop()
    hub.manager.shutdown()


def test_client_handshakes_with_board_info(served):
    fake, _hub, client = served
    esp = client()
    assert esp.info is not None
    assert esp.info.mac == "24:a1:60:12:34:56"  # the fake's MAC, relayed via SYS_READY


def test_two_clients_drive_the_same_board(served):
    fake, _hub, client = served
    a, b = client(), client()

    a.gpio.mode(2, "output")
    a.gpio.write(2, 1)
    assert fake.gpio_levels[2] == 1

    b.gpio.mode(4, "output")
    b.gpio.write(4, 0)
    assert fake.gpio_levels[4] == 0
    # Each client kept its own seq-space; replies landed on the right socket.
    assert a.gpio.read(2) == 1
    assert b.gpio.read(4) == 0


def test_remote_error_is_relayed(served):
    from espbridge.errors import RemoteError

    _fake, _hub, client = served
    esp = client()
    # Writing a pin that was never set to output -> firmware BAD_PIN, surfaced
    # as a RemoteError on the client just like a direct link.
    with pytest.raises(RemoteError):
        esp.gpio.write(9, 1)


def test_oled_frames_over_hub(served):
    """The robot's hot path: OLED frames (fire-and-forget writes + a final waited
    sync) pushed through the relay reach the board, sequenced."""
    from PIL import Image

    from espbridge.drivers.oled import OLED

    fake, _hub, client = served
    fake.i2c_devices[0x3C] = bytes(1024)  # an OLED answering on the bus
    esp = client()
    oled = OLED(esp, sda=21, scl=22, width=128, height=64)
    oled.show(Image.new("1", (128, 64), 1))
    assert fake.i2c_writes  # frame bytes made it to the board through the hub


def test_board_events_fan_out_to_all_clients(served):
    fake, _hub, client = served
    a, b = client(), client()
    got_a, got_b = [], []
    a.on_event(None, lambda fr: got_a.append(fr.cmd))
    b.on_event(None, lambda fr: got_b.append(fr.cmd))

    fake.emit(C.WIFI_SCAN_DONE, bytes([0]))

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not (got_a and got_b):
        time.sleep(0.02)
    assert C.WIFI_SCAN_DONE in got_a
    assert C.WIFI_SCAN_DONE in got_b
