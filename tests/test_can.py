"""CAN/TWAI: begin/send/recv/callbacks/status."""
import struct

import pytest

from espbridge import constants as C
from espbridge.can import Message


def emit_can(fw, msg_id, data, flags=0):
    fw.emit(C.TWAI_RX_EVT, bytes([flags]) + struct.pack(">I", msg_id) + data)


def test_begin_and_send(bridge, fw):
    bridge.can.begin(tx=21, rx=22, bitrate=250_000)
    assert fw.twai_config == (21, 22, 0, 4, None)
    bridge.can.send(0x123, b"\x01\x02")
    assert fw.twai_sent == [(0, 0x123, b"\x01\x02")]


def test_send_extended_and_message_object(bridge, fw):
    bridge.can.begin(tx=21, rx=22)
    bridge.can.send(Message(0x18DAF110, b"\xAA", extended=True))
    flags, mid, data = fw.twai_sent[0]
    assert (flags, mid, data) == (1, 0x18DAF110, b"\xAA")


def test_send_rejects_long_payload(bridge, fw):
    bridge.can.begin(tx=21, rx=22)
    with pytest.raises(ValueError):
        bridge.can.send(0x1, b"123456789")


def test_filter_passthrough(bridge, fw):
    bridge.can.begin(tx=21, rx=22, accept=(0x100 << 21, 0x001FFFFF))
    assert fw.twai_config[4] == (0x100 << 21, 0x001FFFFF, 1)


def test_recv_polled(bridge, fw):
    can = bridge.can
    emit_can(fw, 0x7E8, b"\x10\x20")
    msg = can.recv(timeout=1.0)
    assert (msg.id, msg.data, msg.extended) == (0x7E8, b"\x10\x20", False)
    assert can.recv(timeout=0.01) is None


def test_callbacks_bypass_queue(bridge, fw):
    import time
    got = []
    can = bridge.can
    can.on_message(got.append)
    emit_can(fw, 0x42, b"\x05", flags=1)
    for _ in range(200):  # event lands on the reader thread
        if got:
            break
        time.sleep(0.005)
    assert got and got[0].id == 0x42 and got[0].extended
    assert can._rx.empty()


def test_status(bridge, fw):
    bridge.can.begin(tx=21, rx=22)
    st = bridge.can.status()
    assert st == {"state": "running", "tx_errors": 2, "rx_errors": 3, "rx_missed": 4}
