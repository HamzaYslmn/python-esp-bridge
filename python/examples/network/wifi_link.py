"""Control a board over Wi-Fi instead of a cable.

Provision once over USB, then talk to it over TCP. The credentials live in the
board's NVS, so it rejoins on every boot — no reflash, and the sketch never
contains your Wi-Fi password.

    uv run python wifi_link.py COM3 "my-ssid" "my-password"
    uv run python wifi_link.py            # already provisioned: just connect

Past a handful of boards use many_boards.py — dialing home beats hunting IPs.
"""
import statistics
import sys
import time

import espbridge
from espbridge import Bridge

port = sys.argv[1] if len(sys.argv) > 1 else None
ssid = sys.argv[2] if len(sys.argv) > 2 else None
password = sys.argv[3] if len(sys.argv) > 3 else ""

if port and ssid:
    with Bridge(port=port, ble=False) as usb:
        print(f"provisioning {usb.info.mac} for {ssid!r} ...")
        usb.wifi.link_setup(ssid, password)          # stored in NVS, starts now
        for _ in range(40):
            st = usb.wifi.link_status()
            if st.ip != "0.0.0.0":
                print("board is listening on", f"{st.ip}:{st.port}")
                break
            time.sleep(0.5)
        else:
            raise SystemExit("board never joined the network — check the password")

print("\nlooking for bridges on the LAN ...")
# Retried: a board that just joined has an IP a second or two before it answers
# the discovery broadcast, so provisioning and finding it in one run would race.
for _ in range(10):
    found = espbridge.find_wifi_devices()
    if found:
        break
    time.sleep(1)
for dev in found:
    print("   ", dev)

# wifi=True pins the link: the first board that answers the discovery broadcast.
# Pass a name or MAC — Bridge("relays", wifi=True) — to pin one, or
# host="192.168.1.50" when broadcast doesn't reach at all (VPNs, subnets, client
# isolation). Bridge.all(wifi=True) takes the whole fleet, dial-home ones too.
with Bridge(wifi=True) as esp:
    print(f"\nconnected to {esp.info.ident} over Wi-Fi, fw "
          f"{'.'.join(map(str, esp.info.fw_version))}")

    esp.gpio.mode(2, "output")
    for _ in range(3):
        esp.gpio.write(2, 1)
        time.sleep(0.2)
        esp.gpio.write(2, 0)
        time.sleep(0.2)

    lat = sorted(esp.ping() * 1000 for _ in range(50))
    print(f"round trip: median {statistics.median(lat):.1f} ms, "
          f"best {lat[0]:.1f} ms, worst {lat[-1]:.1f} ms")
    print("(a board with BLE also running keeps Wi-Fi modem sleep on, which "
          "roughly triples that median — turn BLE off for a latency-critical link)")
    print("heap:", esp.free_heap()["free"], "B free")
