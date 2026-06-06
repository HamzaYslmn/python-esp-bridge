"""Blink an LED over Bluetooth — no USB cable.

Power the board from anything (USB charger, battery), then:

    uv run ble_blink.py                  # the only advertising bridge
    uv run ble_blink.py relays           # by custom name (espbridge set-name)
    uv run ble_blink.py c0:49:ef:d0:3f:e0   # or by MAC

The password defaults to "espbridge" — change it at the top of
firmware/firmware.ino (#define BRIDGE_PASSWORD) and reflash.
"""
import sys
import time

from espbridge import Bridge

target = sys.argv[1] if len(sys.argv) > 1 else True

with Bridge(ble=target, password="espbridge") as esp:
    print(f"connected over Bluetooth: {esp.info.name or esp.info.mac} "
          f"({esp.info.chip.name}), ping {esp.ping() * 1000:.1f} ms")
    esp.gpio.mode(2, "output")
    for _ in range(10):
        esp.gpio.write(2, 1)
        time.sleep(0.25)
        esp.gpio.write(2, 0)
        time.sleep(0.25)
    print("done")
