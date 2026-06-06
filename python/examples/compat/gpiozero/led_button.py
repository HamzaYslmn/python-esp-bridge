"""gpiozero on ESP32 pins — LED + Button exactly like on a Raspberry Pi.

Wire a button between GPIO4 and GND (internal pull-up is used), LED on GPIO2
(most DevKits have one on-board).
"""
from signal import pause

from gpiozero import LED, Button

from espbridge import Bridge
from espbridge.compat.gpiozero import EspBridgeFactory

esp = Bridge()
factory = EspBridgeFactory(esp)

led = LED(2, pin_factory=factory)
button = Button(4, bounce_time=0.03, pin_factory=factory)

button.when_pressed = led.on
button.when_released = led.off

print("press the button (GPIO4) to light the LED (GPIO2) — Ctrl+C to quit")
try:
    pause()
except KeyboardInterrupt:
    pass
finally:
    led.close()
    button.close()
    factory.close()
    esp.close()
