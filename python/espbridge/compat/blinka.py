"""CircuitPython / Adafruit-Blinka compatible I2C, SPI and DigitalInOut.

Implements the same duck-typed protocols as ``busio.I2C``, ``busio.SPI`` and
``digitalio.DigitalInOut``, so the hundreds of ``adafruit-circuitpython-*``
driver libraries run unchanged through the bridge:

    pip install adafruit-circuitpython-bme280

    from espbridge import Bridge
    from espbridge.compat.blinka import I2C
    from adafruit_bme280.basic import Adafruit_BME280_I2C

    with Bridge() as esp:
        bme = Adafruit_BME280_I2C(I2C(esp))
        print(bme.temperature, bme.relative_humidity)

SPI devices work the same way, with CS handled by ``DigitalInOut`` exactly as
``adafruit_bus_device.spi_device.SPIDevice`` expects.
No Adafruit package is imported here — only their wire protocols are spoken.
"""
from __future__ import annotations

import threading


class I2C:
    """busio.I2C-compatible bus on the ESP32's I2C."""

    def __init__(self, esp, *, sda: int = 21, scl: int = 22,
                 frequency: int = 400_000, bus: int = 0, init: bool = True):
        self._i2c = esp.i2c
        self._bus = bus
        self._lock = threading.Lock()
        if init:
            self._i2c.init(sda=sda, scl=scl, freq=frequency, bus=bus)

    # -- locking protocol (CircuitPython drivers acquire and release these
    #    around every transaction to prevent concurrent access) ----------------
    def try_lock(self) -> bool:
        return self._lock.acquire(blocking=False)

    def unlock(self) -> None:
        self._lock.release()

    # -- transfers -------------------------------------------------------------
    def scan(self) -> list[int]:
        return self._i2c.scan(self._bus)

    def writeto(self, address: int, buffer, *, start: int = 0, end=None) -> None:
        end = len(buffer) if end is None else end
        self._i2c.write(address, bytes(buffer[start:end]), self._bus)

    def readfrom_into(self, address: int, buffer, *, start: int = 0, end=None) -> None:
        end = len(buffer) if end is None else end
        data = self._i2c.read(address, end - start, self._bus)
        buffer[start:end] = data

    def writeto_then_readfrom(self, address: int, out_buffer, in_buffer, *,
                              out_start: int = 0, out_end=None,
                              in_start: int = 0, in_end=None) -> None:
        out_end = len(out_buffer) if out_end is None else out_end
        in_end = len(in_buffer) if in_end is None else in_end
        data = self._i2c.write_read(address, bytes(out_buffer[out_start:out_end]),
                                    in_end - in_start, self._bus)
        in_buffer[in_start:in_end] = data

    def deinit(self) -> None:
        self._i2c.deinit(self._bus)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.deinit()


class SPI:
    """busio.SPI-compatible bus on the ESP32's SPI.

    Chip select is *not* handled here — pass a ``DigitalInOut`` to
    ``adafruit_bus_device.spi_device.SPIDevice``, exactly like on a Pi.
    """

    def __init__(self, esp, *, sck: int = 18, mosi: int = 23, miso: int = 19,
                 host: int = 0):
        self._spi = esp.spi
        self._host = host
        self._pins = (sck, miso, mosi)
        self._lock = threading.Lock()
        self._frequency = 100_000
        self.configure()  # apply CircuitPython's default configuration: 100 kHz, mode 0

    def try_lock(self) -> bool:
        return self._lock.acquire(blocking=False)

    def unlock(self) -> None:
        self._lock.release()

    def configure(self, *, baudrate: int = 100_000, polarity: int = 0,
                  phase: int = 0, bits: int = 8) -> None:
        if bits != 8:
            raise ValueError("only 8-bit SPI transfers are supported")
        sck, miso, mosi = self._pins
        self._frequency = baudrate
        self._spi.init(sck=sck, miso=miso, mosi=mosi, freq=baudrate,
                       mode=(polarity << 1) | phase, host=self._host)

    @property
    def frequency(self) -> int:
        return self._frequency

    def write(self, buffer, *, start: int = 0, end=None) -> None:
        end = len(buffer) if end is None else end
        self._spi.transfer(bytes(buffer[start:end]), host=self._host)

    def readinto(self, buffer, *, start: int = 0, end=None, write_value: int = 0) -> None:
        end = len(buffer) if end is None else end
        rx = self._spi.transfer(bytes([write_value]) * (end - start), host=self._host)
        buffer[start:end] = rx

    def write_readinto(self, out_buffer, in_buffer, *,
                       out_start: int = 0, out_end=None,
                       in_start: int = 0, in_end=None) -> None:
        out_end = len(out_buffer) if out_end is None else out_end
        in_end = len(in_buffer) if in_end is None else in_end
        if (out_end - out_start) != (in_end - in_start):
            raise ValueError("buffer slices must be of equal length")
        rx = self._spi.transfer(bytes(out_buffer[out_start:out_end]), host=self._host)
        in_buffer[in_start:in_end] = rx

    def deinit(self) -> None:
        self._spi.deinit(self._host)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.deinit()


class Direction:
    INPUT = "input"
    OUTPUT = "output"


class Pull:
    UP = "up"
    DOWN = "down"


class DigitalInOut:
    """digitalio.DigitalInOut-compatible pin (used for CS lines and plain GPIO)."""

    def __init__(self, esp, pin: int):
        self._gpio = esp.gpio
        self.pin = pin
        self._direction = Direction.INPUT
        self._pull = None
        self._value = False
        self._gpio.mode(pin, "input")

    def switch_to_output(self, value: bool = False, drive_mode=None) -> None:
        self._direction = Direction.OUTPUT
        self._gpio.mode(self.pin, "output")
        self.value = value

    def switch_to_input(self, pull=None) -> None:
        self._direction = Direction.INPUT
        self._pull = pull
        self._gpio.mode(self.pin, {Pull.UP: "input_pullup",
                                   Pull.DOWN: "input_pulldown"}.get(pull, "input"))

    @property
    def direction(self):
        return self._direction

    @direction.setter
    def direction(self, value) -> None:
        if value == Direction.OUTPUT:
            self.switch_to_output()
        else:
            self.switch_to_input()

    @property
    def value(self) -> bool:
        if self._direction == Direction.OUTPUT:
            return self._value
        return bool(self._gpio.read(self.pin))

    @value.setter
    def value(self, val: bool) -> None:
        self._value = bool(val)
        self._gpio.write(self.pin, self._value)

    @property
    def pull(self):
        return self._pull

    @pull.setter
    def pull(self, value) -> None:
        self.switch_to_input(value)

    def deinit(self) -> None:
        self._gpio.mode(self.pin, "input")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.deinit()
