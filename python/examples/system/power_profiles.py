"""Battery power profiles: trade speed for runtime with one call.

power_mode() bundles the board's power knobs:

    "performance"  240 MHz CPU + (over Bluetooth) lowest-latency link
    "battery"       80 MHz CPU + (over Bluetooth) radio wakes ~2x/s not ~130x/s

Over USB only the CPU knob applies; over Bluetooth the link profile is the
bigger win (see docs/PROTOCOL.md "Power" for measured numbers). ESP-NOW
receive duty is a third, separate knob: esp.espnow.power_save(window_ms).

    uv run system/power_profiles.py
"""
import time

from espbridge import Bridge


def avg_ping_ms(esp, n: int = 50) -> float:
    t0 = time.perf_counter()
    for _ in range(n):
        esp.ping()
    return (time.perf_counter() - t0) / n * 1000


with Bridge() as esp:
    for mode in ("performance", "battery", "performance"):
        applied = esp.power_mode(mode)
        link = applied["ble_link"] or "USB (no BLE knob)"
        print(f"{mode:>12}: CPU {applied['cpu_mhz']} MHz, link {link}, "
              f"ping {avg_ping_ms(esp):.2f} ms, "
              f"heap {esp.free_heap()['free']} B free")

# The knobs also work individually:
#   esp.cpu_freq(80)                  # just the CPU (80/160/240)
#   esp.link_power("battery")         # just the BLE link (needs a BLE session)
#   esp.espnow.power_save(0)          # ESP-NOW RX off entirely (TX still works)
