"""Edge interrupts: push-button on GPIO 4 (wired to GND, internal pull-up)."""
import time

from espbridge import Bridge

BUTTON = 4

with Bridge() as esp:
    esp.gpio.mode(BUTTON, "input_pullup")
    esp.gpio.watch(BUTTON, edge="falling", debounce_ms=30,
                 callback=lambda ev: print(f"pressed! (t={ev.millis} ms)"))
    print("waiting for button presses for 30 s ...")
    time.sleep(30)
    esp.gpio.unwatch(BUTTON)
