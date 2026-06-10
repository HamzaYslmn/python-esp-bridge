"""Capacitive touch: read a pad, then let the board push touch events.

GPIO 4 is touch channel T0 on the classic ESP32 — bare wire or PCB pad works.
The value DROPS when touched (capacitance rises). Part two hands detection to
the on-device watch engine, so Python gets a callback without polling:

    uv run basics/touch_read.py
"""
import time

from espbridge import Bridge

PAD = 4

with Bridge() as esp:
    # 1. Poll it from Python to find your thresholds.
    idle = esp.touch.read(PAD)
    print(f"pad GPIO{PAD}: idle ~{idle} — touch it (5 s) ...")
    for _ in range(10):
        print(f"  {esp.touch.read(PAD)}")
        time.sleep(0.5)

    # 2. Or let the ESP32 do the sampling: one event per touch, no polling.
    threshold = idle * 2 // 3
    esp.watch.add("touch", pin=PAD, below=threshold, period_ms=50,
                  callback=lambda ev: print(f"  touched! (value {ev.value})"))
    print(f"watch armed (below {threshold}) — touch the pad (10 s) ...")
    time.sleep(10)
