# Writing your own device driver

python-esp-bridge ships drivers for a handful of common parts — DHT, OLED,
NeoPixel, DS18B20, HC-SR04, IR, stepper. Those are **reference
implementations, not the whole story.** There are far more sensors, displays
and protocols than any one library can carry, so the design makes *your* device
a first-class citizen: you add it in Python, on the host, with **no firmware
change and no reflashing.**

This guide shows the three ways to do that, from "just write a class" to
"publish a pip package others install."

## The idea: firmware = primitives, drivers = Python

The firmware exposes only generic hardware **primitives**:

| primitive | `esp.` API | good for |
|-----------|-----------|----------|
| GPIO | `esp.gpio` | bit-banged protocols, chip selects, resets |
| I2C | `esp.i2c` | most sensors and displays |
| SPI | `esp.spi` | displays, ADCs, flash, radios |
| UART | `esp.uart` | GPS, modems, serial sensors |
| RMT | `esp.rmt` | precise pulse trains — WS2812, IR, DHT, ultrasonic |
| 1-Wire | `esp.onewire` | DS18B20 and friends |
| CAN / I2S | `esp.can` / `esp.i2s` | automotive buses, audio |

A **driver** is just a Python class that speaks a device's protocol *over those
primitives*. Every bundled driver lives in
[`espbridge/drivers/`](../python/espbridge/drivers/):
[`drivers/dht.py`](../python/espbridge/drivers/dht.py) is 75 lines over
`esp.rmt`; [`drivers/oled.py`](../python/espbridge/drivers/oled.py) is built on
`esp.i2c`. Because the protocol logic lives in Python, it's easy to read, test
on the host, and change without touching C++.

The contract is one rule:

> **A driver's constructor takes the `Bridge` as its first argument** and uses
> `bridge.i2c` / `bridge.spi` / `bridge.rmt` / … — it never touches the wire
> protocol directly.

That's it. Everything below is convenience on top of that contract.

## 1. Just write a class

The simplest driver needs no registration at all — import it and instantiate
it. Here's a complete driver for a generic I2C temperature sensor:

```python
class MyTempSensor:
    def __init__(self, bridge, address=0x48, *, bus=0):
        self._i2c = bridge.i2c
        self._addr = address
        self._bus = bus

    def read_c(self) -> float:
        # register 0x00 holds a big-endian 12-bit temperature in 1/16 °C
        hi, lo = self._i2c.read_reg(self._addr, 0x00, 2, self._bus)
        raw = ((hi << 8 | lo) >> 4)
        return raw * 0.0625
```

```python
from espbridge import Bridge

with Bridge() as esp:
    esp.i2c.init(sda=21, scl=22)
    print(MyTempSensor(esp).read_c())
```

That already works, everywhere, for anyone. The next two steps only add
*ergonomics and discoverability*.

### Tips that apply to every driver

- **Take the bus number / pins as keyword args** with sensible defaults, so the
  same driver works on either I2C bus and the user can override.
- **Don't re-init the bus** if you can avoid it — several devices usually share
  one bus. The bundled [`pcf8574`](../python/espbridge/drivers/pcf8574.py) only
  calls `i2c.init()` when the caller explicitly passes pins.
- **Pure decode/encode helpers as module-level functions** (like `dht.decode`)
  are trivial to unit-test without any hardware. See the fake-firmware tests in
  [`python/tests/`](../python/tests/).
- **Check capabilities** for anything chip-specific:
  `bridge.require(Cap.DAC, "my feature")` raises a clear error on chips that
  lack it.
- **Raise `ValueError`** for bad arguments and let bridge/`RemoteError`
  propagate for bus faults — callers already handle those.

## 2. Register a name → `esp.<name>(...)`

Register your class under a short name and the bridge gives you a factory with
the connection already bound:

```python
from espbridge import register_driver

register_driver("mytemp", MyTempSensor)

with Bridge() as esp:
    esp.i2c.init(sda=21, scl=22)
    print(esp.mytemp(address=0x48).read_c())   # == MyTempSensor(esp, address=0x48)
```

`esp.mytemp` is a *factory bound to that bridge*; calling it constructs the
driver with `esp` supplied as the first argument. `register_driver(name, cls,
replace=True)` lets you shadow a bundled driver with your own. Names that
collide with a built-in peripheral (`gpio`, `i2c`, …) are rejected.

This is the same mechanism the built-in drivers use — `esp.dht(4)` is just
`DHT(esp, 4)`.

## 3. Ship a pip package (plugin)

To make your driver appear on every bridge *without anyone importing or calling
`register_driver`*, advertise an **entry point** in the `espbridge.drivers`
group from your package's `pyproject.toml`:

```toml
# pyproject.toml of your package, e.g. "espbridge-mytemp"
[project]
name = "espbridge-mytemp"
dependencies = ["python-esp-bridge"]

[project.entry-points."espbridge.drivers"]
mytemp = "espbridge_mytemp:MyTempSensor"
```

Now anyone who `pip install espbridge-mytemp` gets:

```python
with Bridge() as esp:
    esp.mytemp(address=0x48).read_c()      # discovered automatically
```

Discovery is lazy: the entry point is only resolved (your module imported) the
first time someone touches `esp.mytemp`, so plugins cost nothing at startup. An
explicit `register_driver()` always wins over a discovered plugin of the same
name.

## Contributing back to the repo

Small, broadly useful drivers are welcome right in the
[`espbridge/drivers/`](../python/espbridge/drivers/) package — that's where
*every* driver lives, the bundled ones and yours alike. See the folder's
[`README.md`](../python/espbridge/drivers/README.md) for the checklist (drop a
module in, add one line to `_BUNDLED` in
[`drivers/__init__.py`](../python/espbridge/drivers/__init__.py), add a test,
document it). [`pcf8574.py`](../python/espbridge/drivers/pcf8574.py) is a
minimal, commented example to copy.

## Already have a driver from another ecosystem?

You often don't need to write anything. python-esp-bridge speaks the wire
protocols of the popular Python hardware libraries, so existing drivers run
unchanged with the ESP32's pins standing in for the Pi's — see the
**compat** shims in the [main README](../README.md#use-the-libraries-you-already-know):

- **Adafruit CircuitPython** (hundreds of sensors/displays) via
  `espbridge.compat.blinka` (`I2C`, `SPI`, `DigitalInOut`).
- **luma.oled / luma.lcd** displays via `espbridge.compat.luma`.
- **gpiozero** via the `EspBridgeFactory` pin factory.
- **smbus2** and **RPi.GPIO** drop-in shims.

Write a native driver when you want tight control or a clean API; reach for a
compat shim when a battle-tested driver already exists.

## Listing what's available

```bash
espbridge drivers          # bundled drivers + installed plugins
```

```python
import espbridge
espbridge.driver_names()   # ['dht', 'ds18b20', 'hcsr04', 'mytemp', 'oled', ...]
```
