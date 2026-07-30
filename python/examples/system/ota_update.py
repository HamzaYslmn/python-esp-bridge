"""Reflash the firmware over the link — USB or Bluetooth, no boot button.

1. Flash once over USB with Partition Scheme = "Minimal SPIFFS (1.9MB APP
   with OTA)".
2. Export a compiled binary (Sketch > Export Compiled Binary), then:

       uv run ota_update.py path/to/Bridge.ino.bin
       uv run ota_update.py path/to/Bridge.ino.bin relays   # a specific board

Works over Bluetooth too — wireless firmware updates.
"""
import sys

from espbridge import Bridge

if len(sys.argv) < 2:
    raise SystemExit(f"usage: {sys.argv[0]} <firmware.bin> [board-name-or-mac]")
binfile, board = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)

with Bridge(board) as esp:
    old = esp.info.fw_version
    print(f"current firmware: {'.'.join(map(str, old))}")
    esp.ota.flash(binfile, progress=lambda done, total: print(
        f"\r{done}/{total} B ({100 * done // total}%)", end="", flush=True))
    print("\nflashed; board is rebooting into the new firmware")
