"""Drive several ESP32 bridges at once.

One-time setup — give each board a persistent name (stored in its flash):

    espbridge -p COM7 set-name sensors
    espbridge -p COM8 set-name relays
"""
import espbridge
from espbridge import Bridge

# Address a specific board by its stored name (or mac="aa:bb:..."):
with Bridge(name="relays") as esp:
    esp.gpio.mode(2, "output")
    esp.gpio.write(2, 1)

# Or open everything that's plugged in:
with espbridge.connect_all() as boards:
    for esp in boards:
        print(f"{esp.info.name or '(unnamed)':12s} {esp.info.chip.name}  {esp.info.mac}")

    boards.by_name("sensors").adc.config(34, atten=11)
    print("light sensor:", boards.by_name("sensors").adc.read_mv(34), "mV")
    boards.by_name("relays").gpio.write(2, 0)
