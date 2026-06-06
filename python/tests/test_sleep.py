"""Deep/light sleep + wake cause."""
import pytest


def test_deep_sleep_timer(bridge, fw):
    bridge.deep_sleep(60)
    assert fw.sleeps == [(0, 60_000_000, -1, 1)]


def test_deep_sleep_wake_pin(bridge, fw):
    bridge.deep_sleep(wake_pin=33, wake_level=0)
    assert fw.sleeps == [(0, 0, 33, 0)]


def test_light_sleep_returns_cause(bridge, fw):
    assert bridge.light_sleep(0.5) == 4  # timer
    assert bridge.light_sleep(wake_pin=0) == 7  # gpio
    assert bridge.wake_cause() == 7


def test_sleep_without_wake_source_rejected(bridge):
    with pytest.raises(ValueError):
        bridge.deep_sleep()
