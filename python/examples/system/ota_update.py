"""Reflash the firmware over the link — USB or Bluetooth, no boot button.

1. Flash once over USB with Partition Scheme = "Minimal SPIFFS (1.9MB APP
   with OTA)".
2. Export a compiled binary (Sketch > Export Compiled Binary), then:

       python ota_update.py path/to/Bridge.ino.bin

Works over `Bridge(ble=...)` too — wireless firmware updates.
"""
import sys

from espbridge import Bridge

binfile = sys.argv[1]

with Bridge() as esp:
    old = esp.info.fw_version
    print(f"current firmware: {'.'.join(map(str, old))}")
    esp.ota.flash(binfile, progress=lambda done, total: print(
        f"\r{done}/{total} B ({100 * done // total}%)", end="", flush=True))
    print("\nflashed; board is rebooting into the new firmware")
