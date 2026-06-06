"""Deep sleep: put the board to sleep for 10 s, watch it come back.

wake_cause() works on every build. Entering sleep needs IRAM the BLE link
occupies on classic ESP32 — build the firmware with BRIDGE_ENABLE_BLE 0
there, or use an S2/S3/C3/C6 board.
"""
import time

from espbridge import Bridge
from espbridge.errors import UnsupportedError

with Bridge() as esp:
    print(f"last wake cause: {esp.wake_cause()} (4 = timer)")
    try:
        print("sleeping 10 s...")
        esp.deep_sleep(10)
    except UnsupportedError as e:
        raise SystemExit(
            f"{e}\nOn classic ESP32 the BLE link occupies the IRAM that entering "
            "sleep needs. Reflash with BRIDGE_ENABLE_BLE 0, or use an S2/S3/C3/C6 board."
        )

print("board is asleep; reconnecting after it wakes...")
time.sleep(12)
with Bridge() as esp:
    # The board woke itself on the 10 s timer and rebooted. Note: opening the
    # USB port pulses the ESP32 auto-reset, so wake_cause() now reads power-on
    # (0); on a battery board with no host toggling DTR/RTS it reads 4 (timer).
    print(f"back! board responsive again — {esp.info.chip.name}, "
          f"wake cause {esp.wake_cause()} (USB reset masks the timer cause)")
