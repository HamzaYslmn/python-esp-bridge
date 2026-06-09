"""Bridge sub-API dispatch: lazy creation, caching, dir() discoverability."""
import pytest


def test_subapi_lazy_and_cached(bridge):
    g1 = bridge.gpio
    g2 = bridge.gpio
    assert g1 is g2  # same object: sub-API is cached on the instance after first access
    assert "gpio" in bridge.__dict__  # once cached, __getattr__ is no longer called for it


def test_every_subapi_constructs(bridge):
    for name in bridge._SUBAPIS:
        assert getattr(bridge, name) is not None


def test_unknown_attribute_raises(bridge):
    with pytest.raises(AttributeError):
        bridge.does_not_exist


def test_dir_lists_subapis(bridge):
    listed = dir(bridge)
    for name in bridge._SUBAPIS:
        assert name in listed
