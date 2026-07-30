"""ESP-NOW broadcast, fully wireless — Bluetooth to the boards, ESP-NOW between them.

Scans for every board advertising the bridge service, connects to each over
Bluetooth (no USB cables; default password "espbridge"), puts them in the
battery power profile, and makes them all broadcast to ff:ff:ff:ff:ff:ff and
listen — boards in range just talk:

    uv run espnow_broadcast.py                # find and run every board
    uv run espnow_broadcast.py relays spare2  # only these (name or MAC)

power_mode("battery") = 80 MHz CPU + relaxed BLE link (radio wakes ~2x/s
instead of ~130x/s) — and ESP-NOW delivery actually improves, because a
relaxed BLE link leaves more radio time for the Wi-Fi side. Messages just
arrive in ~0.5 s batches instead of instantly. A board never hears its own
broadcasts; with one board it still pairs up with any other board in range.
"""
import sys
import time

from espbridge import Bridge, find_ble_devices

picks = sys.argv[1:] or [d.mac for d in find_ble_devices()]
if not picks:
    sys.exit("no boards advertising over Bluetooth")

boards = []
try:
    for sel in picks:
        esp = Bridge(sel, ble=True, password="espbridge")
        boards.append(esp)
        mac = esp.espnow.begin()       # set up at full speed...
        esp.power_mode("battery")      # ...then relax the radio
        me = esp.info.ident
        print(f"{me}: Bluetooth link up (battery profile), ESP-NOW on {mac}")
        esp.espnow.on_receive(
            lambda src, data, rssi, me=me:
                print(f"{me} heard [{src} {rssi}dBm] {data.decode(errors='replace')}")
        )
    if len(boards) == 1:
        print("one board found — broadcasting anyway; it will talk to any "
              "other board in range running this script")

    n = 0
    while True:
        n += 1
        for esp in boards:
            esp.espnow.broadcast(f"{esp.info.ident}: tick {n}".encode())
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    # Free each board's Wi-Fi driver (~50 KB) before exiting. Over BLE nothing
    # resets the board between sessions, so a driver left resident would leave
    # ~8 KB of heap and cripple every later Bluetooth session.
    for esp in boards:
        esp.espnow.end()
        esp.close()
