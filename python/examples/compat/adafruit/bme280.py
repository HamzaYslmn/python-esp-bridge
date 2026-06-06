"""Any Adafruit CircuitPython driver through the bridge — BME280 shown here.

The espbridge.compat.blinka shim speaks busio/digitalio's wire protocols, so
the hundreds of adafruit-circuitpython-* sensor/display drivers run unchanged.

    uv run adafruit/bme280.py

Wiring: BME280 SDA -> GPIO21, SCL -> GPIO22, VIN -> 3V3, GND -> GND.
"""
import time

from adafruit_bme280.basic import Adafruit_BME280_I2C

from espbridge import Bridge
from espbridge.compat.blinka import I2C

with Bridge() as esp:
    i2c = I2C(esp)                      # drop-in for busio.I2C / board.I2C()
    bme = Adafruit_BME280_I2C(i2c)      # the Adafruit driver, unmodified

    for _ in range(10):
        print(f"{bme.temperature:6.2f} °C   {bme.relative_humidity:5.1f} %RH   "
              f"{bme.pressure:7.1f} hPa")
        time.sleep(1)
