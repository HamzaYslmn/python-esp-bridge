"""Turn the ESP32 into a Wi-Fi access point.

Starts an open-or-passworded AP, keeps it up for a minute (join it from your
phone — clients usually get 192.168.4.x), then shuts it down to give the
~50 KB of radio heap back:

    uv run network/wifi_ap.py

The AP coexists with the rest of the radio: ESP-NOW keeps working while it's
up (the firmware keeps the STA interface alive alongside the AP), and a
station connection can run at the same time (AP+STA mode).
"""
import time

from espbridge import Bridge

# ble=False: on the classic ESP32 the Wi-Fi radio can't come up over a live
# Bluetooth session (the firmware refuses with NO_MEM), so use the cable.
with Bridge(ble=False) as esp:
    ip = esp.wifi.ap_start("espbridge-demo", "letmein123", channel=6)
    print(f"AP 'espbridge-demo' up at {ip} — join it (60 s) ...")
    try:
        for remaining in range(60, 0, -10):
            print(f"  {remaining}s left, heap {esp.free_heap()['free']} B")
            time.sleep(10)
    finally:
        esp.wifi.ap_stop()
        print(f"AP stopped, heap {esp.free_heap()['free']} B")
