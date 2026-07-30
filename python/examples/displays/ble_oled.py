"""OLED drawing over Bluetooth — no USB cable, no COM port.

Same display + wiring as oled_ssd1306.py (SDA -> GPIO21, SCL -> GPIO22),
but the link is the BLE transport: power the board from a charger or
battery and run this from anywhere in radio range.

    uv run ble_oled.py           # 0.96" SSD1306 / clones
    uv run ble_oled.py relays    # the board with the screen, if you have several
    uv run ble_oled.py relays 2  # 1.3" SH1106 (image shifted sideways? try 1..4)

Password defaults to "espbridge" (set via EspBridge.ble.begin() in the firmware).
BLE moves ~5-20 KB/s, so expect ~1-2 fps for full-frame pushes — fine for
status displays; use USB for animation.
"""
import sys
import time

from espbridge import Bridge
from espbridge.drivers.oled import OLED

board = sys.argv[1] if len(sys.argv) > 1 else None    # name or MAC
colstart = int(sys.argv[2]) if len(sys.argv) > 2 else 0

with Bridge(board, ble=True, password="espbridge") as esp:
    print(f"connected over Bluetooth: {esp.info.ident}")
    oled = OLED(esp, colstart=colstart)

    with oled.draw() as d:
        d.text((0, 10), "Hello over BLE!", fill="white")
    time.sleep(2)

    t0 = time.monotonic()
    frames = 0
    while (elapsed := time.monotonic() - t0) < 30:
        with oled.draw() as d:
            d.text((0, 10), "Hello over BLE!", fill="white")
            d.text((0, 28), f"uptime {elapsed:5.1f} s", fill="white")
            d.text((0, 40), f"frame  {frames}", fill="white")
            d.rectangle((0, 56, int(127 * elapsed / 30), 62), fill="white")
        frames += 1
    print(f"{frames} frames in 30 s ({frames / 30:.1f} fps over BLE+I2C)")
