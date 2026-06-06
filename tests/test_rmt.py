"""RMT sub-API: symbol packing, TX/RX plumbing, trigger forwarding."""
import pytest

from espbridge import constants as C
from espbridge.errors import UnsupportedError
from espbridge.rmt import pack_symbols, unpack_symbols

import fake_firmware


def test_symbol_roundtrip():
    syms = [(1, 9000), (0, 4500), (1, 560), (0, 0x7FFF)]
    assert unpack_symbols(pack_symbols(syms)) == syms


def test_symbol_duration_validated():
    with pytest.raises(ValueError):
        pack_symbols([(1, 0x8000)])
    with pytest.raises(ValueError):
        pack_symbols([(1, 0)])


def test_tx_records_symbols(bridge, fw):
    bridge.rmt.init_tx(5, 1_000_000)
    assert fw.rmt_pins[5] == (0, 1_000_000)
    bridge.rmt.tx(5, [(1, 100), (0, 200)])
    assert fw.rmt_tx == [(5, [(1, 100), (0, 200)])]


def test_tx_requires_init(bridge):
    from espbridge.errors import RemoteError
    with pytest.raises(RemoteError):
        bridge.rmt.tx(9, [(1, 1)])


def test_recv_returns_capture_and_forwards_trigger(bridge, fw):
    bridge.rmt.init_rx(18)
    fw.rmt_capture = [(1, 580), (0, 100)]
    syms = bridge.rmt.recv(18, idle_ticks=30_000, timeout_ms=100,
                           max_symbols=16, trigger=(5, 1, 10))
    assert syms == [(1, 580), (0, 100)]
    assert fw.rmt_recv_args == [(18, 30_000, 100, 16, (5, 1, 10))]


def test_recv_without_trigger(bridge, fw):
    bridge.rmt.init_rx(18)
    bridge.rmt.recv(18, idle_ticks=100)
    assert fw.rmt_recv_args[0][4] is None


def test_loop_and_stop(bridge, fw):
    bridge.rmt.init_tx(12)
    bridge.rmt.tx_loop(12, [(1, 10), (0, 990)])
    assert fw.rmt_loops[12] == [(1, 10), (0, 990)]
    bridge.rmt.tx_stop(12)
    assert 12 not in fw.rmt_loops


def test_carrier(bridge, fw):
    bridge.rmt.init_tx(4)
    bridge.rmt.carrier(4, 38_000, duty_pct=33)
    assert fw.rmt_carrier[4] == (38_000, 33, 1)


def test_cap_absent_raises(monkeypatch):
    from espbridge.bridge import Bridge
    monkeypatch.setattr(fake_firmware, "CAPS",
                        fake_firmware.CAPS & ~C.Cap.RMT)
    f = fake_firmware.FakeFirmware()
    f.boot()
    b = Bridge(transport=f.transport, upgrade_baud=False, reset_on_open=False)
    try:
        with pytest.raises(UnsupportedError):
            b.rmt
    finally:
        b.close()
