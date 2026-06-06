"""I2C OLED hello-world — direct espbridge support, zero configuration.

espbridge.oled.OLED drives true SSD1306s *and* the common SH1106 clones sold
as "SSD1306" (the Arduino Adafruit_SSD1306 equivalent — no code on the ESP32,
drawing is plain PIL). Only genuine 1.3" SH1106 modules need a tweak:

    uv run oled_ssd1306.py        # 0.96" SSD1306 / clones: just works
    uv run oled_ssd1306.py 2      # 1.3" SH1106 (image shifted sideways? try 1..4)

Wiring (classic DevKit defaults): SDA -> GPIO21, SCL -> GPIO22, VCC -> 3V3, GND.
"""
import sys
import time

from espbridge import Bridge
from espbridge.oled import OLED

colstart = int(sys.argv[1]) if len(sys.argv) > 1 else 0

with Bridge() as esp:
    oled = OLED(esp, colstart=colstart)

    # --- the Arduino sketch, line for line ---------------------------------
    with oled.draw() as d:
        d.text((0, 10), "Hello, espbridge!", fill="white")
    time.sleep(2)

    # --- bonus: live redraws to show the update rate over the bridge -------
    t0 = time.monotonic()
    frames = 0
    while (elapsed := time.monotonic() - t0) < 10:
        with oled.draw() as d:
            d.text((0, 10), "Hello, espbridge!", fill="white")
            d.text((0, 28), f"uptime {elapsed:5.1f} s", fill="white")
            d.text((0, 40), f"frame  {frames}", fill="white")
            d.rectangle((0, 56, int(127 * elapsed / 10), 62), fill="white")
        frames += 1
    print(f"{frames} frames in 10 s ({frames / 10:.1f} fps over USB+I2C)")
