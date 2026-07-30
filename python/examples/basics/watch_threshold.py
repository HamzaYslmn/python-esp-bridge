"""Push notifications from the ESP32 — no polling, no interrupt.

A *watch rule* lives on the board: you define a condition once, the ESP32
samples it on a background task, and it sends an event **only when the
condition trips**. The same event arrives over Bluetooth or USB.

It's not analog-only — the *source* is whatever you pick. This example arms two
at once:
  * ANALOG:  "ADC pin goes above 200 (on a 0..255 scale)"  -> above=
  * DIGITAL: "GPIO pin reads high"                          -> equals=1
…and the trailing comments show touch, free-heap, ranges and change-by-delta.

Why a watch instead of `gpio.watch()` (edge interrupt)?
  * `gpio.watch()` uses a hardware interrupt — it literally interrupts the CPU,
    and only sees digital 0/1 edges.
  * `watch.add()` is *polled* on a background task (the main loop is never
    interrupted) and works on analog, digital, touch, free-heap, … — and you
    define the trigger condition.

Run:  python watch_threshold.py
"""
import time

import espbridge

ADC_PIN = 34          # analog input to monitor (ADC1 pin; input-only on classic ESP32)
DIGITAL_PIN = 15      # digital input to monitor (any free GPIO)
THRESHOLD_255 = 200   # notify when the 0..255 analog value goes above this
DEADBAND_255 = 8      # must fall this far back before re-arming (kills chatter)

# The ESP32's ADC is 12-bit (0..4095). You think in 0..255, so scale to/from it.
_FS8, _FS12 = 255, 4095
def to_raw(v8):  return round(v8 / _FS8 * _FS12)     # 0..255  -> 0..4095
def to_255(v12): return round(v12 / _FS12 * _FS8)    # 0..4095 -> 0..255


def main():
    # Bluetooth push notifications. Use ble=False for the same thing over USB.
    esp = espbridge.connect(ble=True)
    print(f"connected: {esp.info.ident} "
          f"(fw v{'.'.join(map(str, esp.info.fw_version))})")

    def on_analog(ev):
        # Runs on the bridge's reader thread whenever the analog rule trips.
        v = to_255(ev.value)
        if ev.active:
            print(f"  ⚠  ADC GPIO{ADC_PIN} exceeded {THRESHOLD_255}: now {v}/255 "
                  f"(raw {ev.value}, t={ev.millis} ms)")
        else:
            print(f"  ✓  ADC GPIO{ADC_PIN} back below threshold: {v}/255")

    def on_digital(ev):
        print(f"  {'⚡ HIGH' if ev.active else '· low '}  GPIO{DIGITAL_PIN} "
              f"(t={ev.millis} ms)")

    # Both rules are evaluated on the ESP32 — each is ONE request, then events
    # just arrive. Analog uses a threshold; digital uses an exact match.
    esp.watch.add("adc", pin=ADC_PIN,
                  above=to_raw(THRESHOLD_255),     # fire when value rises above 200/255
                  hysteresis=to_raw(DEADBAND_255), # 8/255 deadband before re-arming
                  period_ms=50, callback=on_analog)
    esp.gpio.mode(DIGITAL_PIN, "input_pulldown")
    esp.watch.add("gpio", pin=DIGITAL_PIN,
                  equals=1,                        # fire when the pin reads high
                  period_ms=20, callback=on_digital)

    print(f"watching ADC GPIO{ADC_PIN} (> {THRESHOLD_255}/255) and digital "
          f"GPIO{DIGITAL_PIN} (high) — sampling happens on the board, not here.\n"
          "Drive either pin to see events. Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)        # your program does its own work; events just arrive
    except KeyboardInterrupt:
        esp.watch.clear()
        print("\nstopped.")


# --- Prefer async/await? The same rule as an async stream -----------------------
#
#     import asyncio
#     from espbridge import AsyncBridge
#
#     async def main():
#         async with AsyncBridge(ble=True) as esp:
#             await esp.watch.add("adc", pin=4, above=to_raw(200),
#                                 hysteresis=to_raw(8), period_ms=50)
#             async for ev in esp.watch_events():
#                 print("crossed" if ev.active else "cleared", to_255(ev.value))
#
#     asyncio.run(main())
#
# Any source + any condition — mix and match:
#     esp.watch.add("gpio",   pin=15, equals=1)            # digital: pin reads high
#     esp.watch.add("gpio",   pin=15, equals=0)            # digital: pin reads low
#     esp.watch.add("gpio",   pin=15, change=1)            # digital: any level change
#     esp.watch.add("adc_mv", pin=34, above=1650)          # analog in millivolts
#     esp.watch.add("touch",  pin=4,  below=40)            # touch pad pressed
#     esp.watch.add("heap",   change=20000)                # free-RAM moved a lot
#     esp.watch.add("adc",    pin=34, outside=(800, 3200)) # left a safe band
#
# On-device actions — the FIRMWARE reacts, no host round trip (~5 ms,
# link-independent, keeps working even if the USB/BLE link drops):
#     esp.watch.add("gpio", pin=15, equals=1,               # thermostat contact
#                   do=("gpio", 26, 1), undo=("gpio", 26, 0))   # relay on/off
#     esp.watch.add("adc",  pin=34, above=3200, period_ms=5,    # over-current
#                   do=("pwm", 13, 0))                          # -> kill motor PWM

if __name__ == "__main__":
    main()
