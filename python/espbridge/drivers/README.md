# `espbridge.drivers` — device drivers

Every device driver lives here, the bundled ones and yours alike. A driver is a
pure-Python class built on the bridge's primitives (I2C, SPI, GPIO, RMT,
1-Wire, …) — no firmware change. [`__init__.py`](__init__.py) is the registry
that makes each one available as `esp.<name>(...)`.

```python
esp.dht(pin=4).read()                 # == DHT(esp, pin=4)
esp.pcf8574(address=0x20).write_port(0xFF)
```

[`dht.py`](dht.py) (75 lines over `esp.rmt`) and [`pcf8574.py`](pcf8574.py) (an
I2C GPIO expander) are deliberately small, well-commented examples — copy
either as a starting point.

## Add your own driver

1. **Drop a module in this folder**, e.g. `drivers/mydevice.py`. A driver is a
   class whose constructor takes the `Bridge` as its first argument and uses
   `bridge.i2c` / `bridge.spi` / `bridge.rmt` / … — never raw protocol bytes
   over the wire. Keep dependencies to the standard library plus what the
   package already requires; gate anything heavier behind a lazy import and an
   optional extra.
2. **Register the name** by adding one line to `_BUNDLED` in
   [`__init__.py`](__init__.py):

   ```python
   "mydevice": "espbridge.drivers.mydevice:MyDevice",
   ```

   The mapping is lazy — the module is imported only when `esp.mydevice` is
   first used — so this costs nothing at import time.
3. **Add a test** under [`python/tests/`](../../tests/) (the existing `test_*`
   driver tests use the `fw`/`bridge` fixtures and a fake firmware; copy
   `test_dht.py` or `test_pcf8574.py`).
4. **Document it** with a row in the table below.

That's the whole pattern — see [`docs/DRIVERS.md`](../../../docs/DRIVERS.md) for
the full guide, including the two lighter-weight options (just instantiate a
class, or `register_driver()` at runtime).

> Prefer to keep your driver in your *own* pip package? You don't need this
> folder at all — advertise an `espbridge.drivers` entry point and it appears on
> every bridge automatically. See [`docs/DRIVERS.md`](../../../docs/DRIVERS.md).

## Drivers

| name | device | bus |
|------|--------|-----|
| `dht` | DHT11 / DHT22 temperature + humidity | RMT |
| `oled` | SSD1306 / SH1106 OLED displays | I2C |
| `neopixel` | WS2812 / SK6812 addressable LEDs | RMT |
| `ds18b20` | DS18B20 / DS18S20 thermometers | 1-Wire |
| `hcsr04` | HC-SR04 ultrasonic rangefinder | RMT |
| `ir_sender` / `ir_receiver` | NEC + raw infrared remotes | RMT |
| `stepper` | A4988 / DRV8825 stepper motors | RMT |
| `pcf8574` | PCF8574 / PCF8574A 8-bit GPIO expander | I2C |
