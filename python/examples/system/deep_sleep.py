"""Deep sleep: put the board to sleep for 10 s, watch it come back.

wake_cause() works on every build. Entering sleep needs IRAM the BLE stack
occupies on classic ESP32 — and that is about how the *firmware* was built,
not how you connect, so a USB cable doesn't help: build with
BRIDGE_ENABLE_BLE 0 there, or use an S2/S3/C3/C6 board.
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
# reset_on_open=False: re-attach WITHOUT pulsing the auto-reset line. The
# default open would reboot the board again and overwrite the wake cause
# with power-on (0); this way wake_cause() still reads 4 (the 10 s timer).
with Bridge(reset_on_open=False) as esp:
    print(f"back! {esp.info.chip.name} woke itself — "
          f"wake cause {esp.wake_cause()} (4 = timer)")

# Lighter option: esp.light_sleep(5) pauses the CPU but keeps RAM and all
# peripheral state, and the same connection resumes when the board wakes.
