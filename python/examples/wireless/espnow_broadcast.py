"""ESP-NOW broadcast, fully wireless — Bluetooth to the board, ESP-NOW onward.

The bridge link is Bluetooth (no USB cable; default password "espbridge", set
via EspBridge.begin()) and the messaging is ESP-NOW: BLE and the
Wi-Fi radio coexisting on one classic ESP32. Every board running the listener
hears every broadcast in radio range, with the sender's MAC and RSSI:

    uv run espnow_broadcast.py send           # broadcast a counter once a second
    uv run espnow_broadcast.py                # listen and print whatever arrives
    uv run espnow_broadcast.py send relays    # pick a board by name (or MAC)

Broadcasts are never ACKed (there is no single receiver to ACK), so the
sender uses the fire-and-forget path.
"""
import sys
import time

from espbridge import Bridge

args = sys.argv[1:]
sending = bool(args) and args[0] == "send"
board = (args[1] if sending else args[0] if args else None) or True

with Bridge(ble=board, password="espbridge") as esp:
    mac = esp.espnow.begin()
    print(f"connected over Bluetooth: {esp.info.name or esp.info.mac}")
    print(f"ESP-NOW up on {mac} — {'broadcasting' if sending else 'listening'}")

    if sending:
        n = 0
        while True:
            n += 1
            esp.espnow.broadcast(f"tick {n}".encode())
            print(f"sent: tick {n}")
            time.sleep(1)
    else:
        while True:
            src, data, rssi = esp.espnow.read()  # blocks until a packet arrives
            print(f"[{src} {rssi}dBm] {data.decode(errors='replace')}")
