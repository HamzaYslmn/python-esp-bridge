"""DS18B20 1-Wire thermometer(s) on GPIO 4 (4.7k pull-up to 3V3 required)."""
import time

from espbridge import Bridge
from espbridge.drivers.ds18b20 import DS18B20

with Bridge() as esp:
    roms = DS18B20.discover(esp, pin=4)
    print(f"found {len(roms)} sensor(s): {roms}")
    if not roms:
        raise SystemExit("nothing on the 1-Wire bus — check GPIO4, 3V3/GND and "
                         "the 4.7k pull-up (the bus reads as idle without it)")
    probes = [DS18B20(esp, 4, rom) for rom in roms]
    try:
        while True:
            for p in probes:
                print(f"{p.rom[:8]}…  {p.read():6.2f} C")
            time.sleep(1)
    except KeyboardInterrupt:
        pass
