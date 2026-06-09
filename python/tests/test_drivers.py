"""Driver registry: register_driver, esp.<name>() bound factory, discovery."""
import pytest

from espbridge import driver_names, drivers, register_driver


@pytest.fixture()
def clean_registry():
    """Snapshot the registry and restore it, so tests don't leak names."""
    saved = dict(drivers._registry)
    saved_loaded = drivers._entry_points_loaded
    yield
    drivers._registry = saved
    drivers._entry_points_loaded = saved_loaded


class _Dummy:
    """A minimal driver: records the bridge and args it was built with."""

    def __init__(self, bridge, a, b=2):
        self.bridge = bridge
        self.a = a
        self.b = b


def test_builtin_names_present():
    names = driver_names()
    for n in ("dht", "oled", "neopixel", "ds18b20", "hcsr04", "stepper",
              "ir_sender", "ir_receiver", "pcf8574"):
        assert n in names


def test_get_driver_resolves_lazy_builtin():
    from espbridge.drivers.dht import DHT

    assert drivers.get_driver("dht") is DHT  # "espbridge.drivers.dht:DHT" string -> class
    assert drivers.get_driver("nope") is None


def test_register_and_bound_factory(bridge, clean_registry):
    register_driver("dummy", _Dummy)
    assert "dummy" in driver_names()

    factory = bridge.dummy
    assert isinstance(factory, drivers.BoundDriver)
    assert factory.cls is _Dummy

    dev = bridge.dummy(7, b=9)        # == _Dummy(bridge, 7, b=9)
    assert isinstance(dev, _Dummy)
    assert dev.bridge is bridge and dev.a == 7 and dev.b == 9


def test_register_rejects_duplicate_without_replace(clean_registry):
    register_driver("dummy", _Dummy)
    with pytest.raises(ValueError, match="already registered"):
        register_driver("dummy", _Dummy.__class__)  # different object
    register_driver("dummy", _Dummy)                 # same object: idempotent, ok

    class _Other:
        def __init__(self, bridge):
            pass

    register_driver("dummy", _Other, replace=True)    # explicit override
    assert drivers.get_driver("dummy") is _Other


def test_register_rejects_reserved_peripheral_name(clean_registry):
    with pytest.raises(ValueError, match="built-in peripheral"):
        register_driver("i2c", _Dummy)


def test_register_rejects_bad_name(clean_registry):
    for bad in ("_private", "has space", "1leading"):
        with pytest.raises(ValueError):
            register_driver(bad, _Dummy)


def test_unknown_driver_attribute_raises(bridge):
    with pytest.raises(AttributeError):
        bridge.totally_unknown_thing


def test_dir_lists_drivers(bridge):
    listed = dir(bridge)
    assert "dht" in listed and "pcf8574" in listed
