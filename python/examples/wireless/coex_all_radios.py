"""All three radios at once: BLE link + Wi-Fi AP + ESP-NOW, one classic ESP32.

Connects over Bluetooth (so BLE is busy being the link), then brings up
ESP-NOW and a Wi-Fi access point on the same board. The IDF coexistence
arbiter shares the 2.4 GHz radio; the firmware's heap guards only refuse a
step that would genuinely starve the BLE link (clean NO_MEM, link stays up).

    uv run wireless/coex_all_radios.py             # first advertising board
    uv run wireless/coex_all_radios.py spare       # by name (or MAC)

Run espnow_broadcast.py on another board to see the ESP-NOW side answer, and
join "espbridge-coex" from your phone to see the AP side. Expect the board to
run heap-thin in this state — big transfers (FS/OTA) will refuse until a
radio is released.
"""
import sys
import time

from espbridge import Bridge

target = sys.argv[1] if len(sys.argv) > 1 else None   # name or MAC

with Bridge(target, ble=True, password="espbridge") as esp:
    def heap(tag):
        print(f"  {tag:<28} {esp.free_heap()['free']:>6} B free, "
              f"ping {esp.ping() * 1000:.0f} ms")

    print(f"BLE link up: {esp.info.ident}")
    heap("radio off")

    mac = esp.espnow.begin()                       # radio #2
    heap(f"+ ESP-NOW ({mac})")

    ip = esp.wifi.ap_start("espbridge-coex", "coex12345")   # radio #3
    heap(f"+ Wi-Fi AP ({ip})")

    print("all three radios live — broadcasting for 30 s (Ctrl+C to stop)")
    try:
        for n in range(30):
            esp.espnow.broadcast(f"coex tick {n}".encode())
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        esp.wifi.ap_stop()
        esp.espnow.end()                           # give the ~50 KB back
        heap("radios released")
