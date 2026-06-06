"""Deep sleep: put the board to sleep for 10 s, watch it come back.

Sleep needs IRAM the BLE link occupies on classic ESP32 — build the
firmware with BRIDGE_ENABLE_BLE 0 there, or use an S2/S3/C3/C6 board.
"""
import time

from espbridge import Bridge

with Bridge() as esp:
    print(f"last wake cause: {esp.wake_cause()} (4 = timer)")
    print("sleeping 10 s...")
    esp.deep_sleep(10)

print("board is asleep; reconnecting after it wakes...")
time.sleep(12)
with Bridge() as esp:
    print(f"back! wake cause: {esp.wake_cause()}")
