"""Polled watch engine: rule encoding, event decode/dispatch, list/remove/clear,
and the old-firmware fallback."""
import struct
import threading
import time

import pytest

from espbridge import WatchEvent, constants as C
from espbridge.errors import UnsupportedError


def test_event_parse_roundtrip():
    ev = WatchEvent.parse(struct.pack(">BBiI", 5, 1, -42, 12345))
    assert (ev.id, ev.state, ev.value, ev.millis) == (5, 1, -42, 12345)
    assert ev.active is True
    assert WatchEvent.parse(b"\x01\x02") is None     # too short


def test_above_with_hysteresis_encoding(fw, bridge):
    wid = bridge.watch.add("adc", pin=4, above=3200, hysteresis=100,
                           period_ms=50, atten=11)
    r = fw.watches[wid]
    assert r["source"] == 0 and r["arg"] == 4 and r["aux"] == 3   # adc, pin4, 11dB
    assert r["cmp"] == 0                                          # above
    assert r["a"] == 3200 and r["b"] == 3100                      # release 100 below
    assert r["period_ms"] == 50
    assert r["flags"] == 0x03                                     # enter|exit, no initial


@pytest.mark.parametrize("kwargs,exp_cmp,exp_ab", [
    (dict(above=200), 0, (200, 200)),
    (dict(below=100), 1, (100, 100)),
    (dict(equals=1), 2, (1, 1)),               # digital high -> degenerate band [1,1]
    (dict(equals=0), 2, (0, 0)),               # digital low
    (dict(between=(10, 90)), 2, (10, 90)),
    (dict(outside=(10, 90)), 3, (10, 90)),
    (dict(change=16), 4, (16, 0)),
])
def test_comparator_mapping(fw, bridge, kwargs, exp_cmp, exp_ab):
    wid = bridge.watch.add("gpio", pin=2, **kwargs)
    r = fw.watches[wid]
    assert r["cmp"] == exp_cmp
    assert (r["a"], r["b"]) == exp_ab


def test_digital_watch_is_first_class(fw, bridge):
    """A GPIO source with equals= is a normal watch — proves it's not analog-only."""
    wid = bridge.watch.add("gpio", pin=15, equals=1, period_ms=20)
    r = fw.watches[wid]
    assert r["source"] == 2 and r["arg"] == 15      # gpio, pin 15
    assert r["cmp"] == 2 and (r["a"], r["b"]) == (1, 1)


def test_source_aliases_and_heap_needs_no_pin(fw, bridge):
    wid = bridge.watch.add("heap", change=5000)
    assert fw.watches[wid]["source"] == 4
    wid2 = bridge.watch.add("adc_mv", pin=34, above=1650)
    assert fw.watches[wid2]["source"] == 1


def test_flags_initial_and_one_shot_edges(fw, bridge):
    wid = bridge.watch.add("adc", pin=4, above=100, initial=True, on_exit=False)
    assert fw.watches[wid]["flags"] == 0x01 | 0x04   # enter + initial, no exit


def test_requires_exactly_one_condition(bridge):
    with pytest.raises(ValueError):
        bridge.watch.add("adc", pin=4)                       # none
    with pytest.raises(ValueError):
        bridge.watch.add("adc", pin=4, above=1, below=2)     # two


def test_non_heap_source_requires_pin(bridge):
    with pytest.raises(ValueError):
        bridge.watch.add("adc", above=100)


def test_callback_fires_on_event(fw, bridge):
    got = []
    fired = threading.Event()
    wid = bridge.watch.add("adc", pin=4, above=100,
                           callback=lambda e: (got.append(e), fired.set()))
    fw.emit(C.WATCH_EVT, struct.pack(">BBiI", wid, 1, 3300, 999))
    assert fired.wait(2.0)                       # reader thread dispatches it
    assert len(got) == 1 and got[0].value == 3300 and got[0].active
    # an event for a different id must not invoke this callback
    fw.emit(C.WATCH_EVT, struct.pack(">BBiI", wid + 9, 1, 1, 1))
    time.sleep(0.05)
    assert len(got) == 1


def test_list_and_remove_and_clear(fw, bridge):
    a = bridge.watch.add("adc", pin=4, above=100)
    b = bridge.watch.add("heap", change=1000)
    ids = {row[0] for row in bridge.watch.list()}
    assert ids == {a, b}
    bridge.watch.remove(a)
    assert {row[0] for row in bridge.watch.list()} == {b}
    bridge.watch.clear()
    assert bridge.watch.list() == []
    assert fw.watches == {}


def test_ids_are_recycled_after_remove(fw, bridge):
    a = bridge.watch.add("adc", pin=4, above=1)
    bridge.watch.remove(a)
    b = bridge.watch.add("adc", pin=4, above=1)
    assert b == a                                   # smallest free id reused


def test_unsupported_firmware_raises(fw, bridge):
    fw.watch_supported = False
    with pytest.raises(UnsupportedError, match="0.5.0"):
        bridge.watch.add("adc", pin=4, above=100)


# ---- on-device actions (do= / undo=, WATCH_ADD2) ----

def test_actions_use_add2_and_encode(fw, bridge):
    wid = bridge.watch.add("gpio", pin=15, equals=1,
                           do=("gpio", 26, 1), undo=("gpio", 26, 0))
    r = fw.watches[wid]
    assert r["enter_action"] == (1, 26, 1)          # gpio write 26 -> 1
    assert r["exit_action"] == (1, 26, 0)


def test_do_only_leaves_exit_action_empty(fw, bridge):
    wid = bridge.watch.add("adc", pin=34, above=3200, do=("pwm", 13, 0))
    r = fw.watches[wid]
    assert r["enter_action"] == (2, 13, 0)          # pwm duty 13 -> 0
    assert r["exit_action"] == (0, 0, 0)            # none


def test_plain_add_carries_no_actions(fw, bridge):
    wid = bridge.watch.add("adc", pin=4, above=100)
    assert "enter_action" not in fw.watches[wid]    # WATCH_ADD, 16-byte payload


def test_bad_action_kind_raises_before_arming(fw, bridge):
    with pytest.raises(ValueError, match="gpio"):
        bridge.watch.add("gpio", pin=2, equals=1, do=("servo", 5, 90))
    assert fw.watches == {}


def test_actions_on_old_firmware_raise_and_roll_back(fw, bridge):
    fw.watch_add2_supported = False
    with pytest.raises(UnsupportedError, match="0.16.0"):
        bridge.watch.add("gpio", pin=2, equals=1, do=("gpio", 26, 1),
                         callback=lambda e: None)
    # the id and callback were rolled back; a plain rule can reuse the slot
    wid = bridge.watch.add("adc", pin=4, above=100)
    assert wid == 1 and wid not in bridge.watch._cbs
