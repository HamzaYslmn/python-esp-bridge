"""Rainbow on a WS2812/NeoPixel strip (data pin -> GPIO 5)."""
import colorsys
import time

from espbridge import Bridge
from espbridge.drivers.neopixel import NeoPixel

PIN, COUNT = 5, 30

with Bridge() as esp:
    strip = NeoPixel(esp, PIN, COUNT, brightness=0.3)
    # WS2812 is write-only, so nothing here can tell you the strip is there:
    # say what is being driven, then look at the LEDs. Ctrl+C to stop.
    print(f"{esp.info.ident}: driving {COUNT} pixels on GPIO{PIN} — Ctrl+C to quit")
    t = 0
    try:
        while True:
            for i in range(COUNT):
                r, g, b = colorsys.hsv_to_rgb((i / COUNT + t) % 1.0, 1, 1)
                strip[i] = (int(r * 255), int(g * 255), int(b * 255))
            strip.show()
            t += 0.01
            time.sleep(0.02)
    except KeyboardInterrupt:
        strip.clear()
