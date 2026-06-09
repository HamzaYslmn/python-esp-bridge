"""Driver registry — make "bring your own device" a first-class path.

A *driver* is any class whose constructor takes a :class:`~espbridge.Bridge`
first and speaks a device's protocol over the bridge's primitives (I2C, SPI,
RMT, 1-Wire, ...). DHT and OLED are reference implementations; no firmware
change is needed to add your own. Three ways to use one:

  - import and instantiate:        ``DHT(esp, pin=4)``
  - register a name:               ``register_driver("dht", DHT)`` -> ``esp.dht(pin=4)``
  - ship a pip package advertising an ``espbridge.drivers`` entry point: it then
    appears as ``esp.<name>(...)`` on every bridge automatically.

``esp.<name>`` is a factory bound to that bridge, so ``esp.dht(4)`` is exactly
``DHT(esp, 4)``. List what's available with :func:`driver_names` or the
``espbridge drivers`` command. Full guide: ``docs/DRIVERS.md``.
"""
from __future__ import annotations

import importlib
import importlib.metadata as _md

from .._log import log

ENTRY_POINT_GROUP = "espbridge.drivers"

# Every bundled driver lives in this package (espbridge/drivers/) and is mapped
# lazily as "module:ClassName" so importing the registry never pulls in Pillow
# (oled) or the RMT-heavy modules until a driver is actually used. Add your own
# by dropping a module beside this one and listing it here (see README.md).
_BUNDLED: dict[str, str] = {
    # over the RMT / 1-Wire / I2C primitives
    "dht": "espbridge.drivers.dht:DHT",
    "oled": "espbridge.drivers.oled:OLED",
    "neopixel": "espbridge.drivers.neopixel:NeoPixel",
    "ds18b20": "espbridge.drivers.ds18b20:DS18B20",
    "hcsr04": "espbridge.drivers.hcsr04:HCSR04",
    "stepper": "espbridge.drivers.stepper:Stepper",
    "ir_sender": "espbridge.drivers.ir:IrSender",
    "ir_receiver": "espbridge.drivers.ir:IrReceiver",
    "pcf8574": "espbridge.drivers.pcf8574:PCF8574",
    # ADC / sensors (I2C)
    "ads1115": "espbridge.drivers.ads1115:ADS1115",     # 16-bit 4-ch ADC
    "bh1750": "espbridge.drivers.bh1750:BH1750",        # ambient light (lux)
    "bme280": "espbridge.drivers.bme280:BME280",        # temp / humidity / pressure
    "mpu6050": "espbridge.drivers.mpu6050:MPU6050",     # 6-axis IMU (accel + gyro)
    "ds3231": "espbridge.drivers.ds3231:DS3231",        # real-time clock
    # actuators (robotics)
    "pca9685": "espbridge.drivers.pca9685:PCA9685",     # 16-ch servo / PWM driver
    "motor": "espbridge.drivers.motor:Motor",           # DC motor H-bridge (L298N/TB6612/DRV8833)
    # audio (I2S)
    "inmp441": "espbridge.drivers.inmp441:INMP441",     # MEMS microphone
    "max98357": "espbridge.drivers.max98357:MAX98357",  # class-D amplifier
    # radios (SPI transceivers)
    "nrf24": "espbridge.drivers.nrf24:NRF24",           # 2.4 GHz transceiver
    "sx127x": "espbridge.drivers.sx127x:SX127x",        # LoRa 433/868/915 (SX1276/RFM95)
    "rfm95": "espbridge.drivers.sx127x:SX127x",         # alias for RFM95/96 modules
    "sx126x": "espbridge.drivers.sx126x:SX126x",        # LoRa 433/868/915 (SX1262/1268)
    "sx128x": "espbridge.drivers.sx128x:SX128x",        # 2.4 GHz LoRa (SX1280/1281)
}

# name -> driver class, or a lazy "module:Class" string, or an EntryPoint.
_registry: dict[str, object] = dict(_BUNDLED)
_entry_points_loaded = False


def _reserved_names() -> frozenset[str]:
    """Peripheral sub-API names (esp.gpio, esp.i2c, ...) a driver may not shadow."""
    from ..bridge import Bridge

    return frozenset(Bridge._SUBAPIS)


def register_driver(name: str, cls: type, *, replace: bool = False) -> None:
    """Register driver ``cls`` under ``name`` so ``esp.<name>(...)`` builds it.

    ``cls.__init__`` must take the Bridge as its first positional argument.
    Raises ``ValueError`` if ``name`` is already taken (pass ``replace=True``
    to override, e.g. to shadow a bundled driver with your own), if it collides
    with a built-in peripheral (``gpio``, ``i2c``, ...), or if it is not a
    valid public attribute name.
    """
    if not name.isidentifier() or name.startswith("_"):
        raise ValueError(
            f"driver name {name!r} must be a valid public attribute name")
    if name in _reserved_names():
        raise ValueError(
            f"{name!r} is a built-in peripheral (esp.{name}) and cannot be a "
            f"driver name")
    existing = _registry.get(name)
    if existing is not None and not replace and existing is not cls:
        raise ValueError(
            f"driver {name!r} is already registered ({_describe(existing)}); "
            f"pass replace=True to override it")
    _registry[name] = cls


def _resolve(obj):
    """Normalise a registry value (class / 'module:Class' / EntryPoint) to a class."""
    if isinstance(obj, str):  # lazy bundled driver
        mod, _, qual = obj.partition(":")
        return getattr(importlib.import_module(mod), qual)
    if isinstance(obj, _md.EntryPoint):  # discovered plugin
        return obj.load()
    return obj  # already a class / callable


def get_driver(name: str):
    """Return the driver class registered under ``name``, or ``None`` if unknown.

    Bundled and entry-point drivers are imported lazily and cached on first use.
    A failed import (e.g. a broken plugin) propagates so the error is visible at
    the point of use rather than being swallowed into an ``AttributeError``.
    """
    obj = _registry.get(name)
    if obj is None:
        _discover_entry_points()
        obj = _registry.get(name)
    if obj is None:
        return None
    cls = _resolve(obj)
    _registry[name] = cls  # cache the resolved class
    return cls


def _discover_entry_points() -> None:
    """Load driver names advertised by installed packages (once, lazily)."""
    global _entry_points_loaded
    if _entry_points_loaded:
        return
    _entry_points_loaded = True
    try:
        eps = _md.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as e:  # pragma: no cover - environment dependent
        log.debug(f"espbridge driver discovery failed: {e}")
        return
    for ep in eps:
        _registry.setdefault(ep.name, ep)  # an explicit register_driver() wins


def driver_names() -> list[str]:
    """Sorted names of every registered driver (bundled + discovered plugins)."""
    _discover_entry_points()
    return sorted(_registry)


def _describe(obj) -> str:
    if isinstance(obj, str):
        return obj.replace(":", ".")
    if isinstance(obj, _md.EntryPoint):
        return f"{obj.value} (plugin)"
    mod = getattr(obj, "__module__", "?")
    return f"{mod}.{getattr(obj, '__qualname__', obj)}"


def driver_source(name: str) -> str:
    """Human-readable origin of a registered driver (for listings)."""
    return _describe(_registry.get(name))


class BoundDriver:
    """A driver class with a bridge pre-bound to its first constructor argument.

    Returned by ``esp.<driver-name>``; call it to build the driver:
    ``esp.dht(pin=4)`` is exactly ``DHT(esp, pin=4)``.
    """

    __slots__ = ("_bridge", "cls", "_name")

    def __init__(self, bridge, cls, name: str):
        self._bridge = bridge
        self.cls = cls
        self._name = name

    def __call__(self, *args, **kwargs):
        return self.cls(self._bridge, *args, **kwargs)

    def __repr__(self) -> str:
        return (f"<espbridge driver {self._name!r} -> "
                f"{self.cls.__module__}.{self.cls.__qualname__} "
                f"(call it: esp.{self._name}(...))>")
