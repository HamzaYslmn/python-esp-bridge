"""Read a DHT22 (or DHT11) temperature/humidity sensor on GPIO 4.

Wiring: VCC->3V3, GND->GND, DATA->GPIO4 (+10k pull-up to 3V3 helps clones).
"""
import time

from espbridge import Bridge

with Bridge() as esp:
    sensor = esp.dht(pin=4, model=22)   # model=11 for the blue DHT11
    try:
        while True:
            temp, hum = sensor.read()
            print(f"{temp:5.1f} C   {hum:5.1f} %RH")
            time.sleep(2)
    except KeyboardInterrupt:
        pass
