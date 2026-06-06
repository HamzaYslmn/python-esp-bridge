"""Blink the on-board LED (GPIO 2 on most classic DevKits)."""
import time

from espbridge import Bridge

LED = 2

with Bridge() as esp:
    print(f"connected: {esp.info.chip.name}, ping {esp.ping() * 1000:.1f} ms")
    esp.gpio.mode(LED, "output")
    for _ in range(10):
        esp.gpio.write(LED, 1)
        time.sleep(0.25)
        esp.gpio.write(LED, 0)
        time.sleep(0.25)
