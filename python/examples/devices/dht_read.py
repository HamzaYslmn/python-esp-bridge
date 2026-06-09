"""Read a DHT22 (or DHT11) temperature/humidity sensor on GPIO 4.

Wiring: VCC->3V3, GND->GND, DATA->GPIO4 (+10k pull-up to 3V3 helps clones).
"""
import time

from espbridge import Bridge
from espbridge.drivers.dht import DHT

with Bridge() as esp:
    sensor = DHT(esp, pin=4, model=22)  # model=11 for the blue DHT11
    while True:
        temp, hum = sensor.read()
        print(f"{temp:5.1f} C   {hum:5.1f} %RH")
        time.sleep(2)
