"""ESP-NOW broadcast, fully wireless — Bluetooth to the board, ESP-NOW onward.

The bridge link is Bluetooth (no USB cable; default password "espbridge", set
via EspBridge.begin()) and the messaging is ESP-NOW: BLE and the Wi-Fi radio
coexisting on one classic ESP32. Every instance both broadcasts and listens —
start it on two (or more) boards and they talk to each other automatically:

    uv run espnow_broadcast.py            # any board over Bluetooth
    uv run espnow_broadcast.py relays     # pick a board by name (or MAC)

Broadcasts are never ACKed (there is no single receiver to ACK), so they ride
the fire-and-forget path. A board never hears its own broadcasts.
"""
import sys
import time

from espbridge import Bridge

board = sys.argv[1] if len(sys.argv) > 1 else True

with Bridge(ble=board, password="espbridge") as esp:
    mac = esp.espnow.begin()
    me = esp.info.name or mac
    print(f"connected over Bluetooth: {esp.info.name or esp.info.mac}")
    print(f"ESP-NOW up on {mac} — broadcasting and listening as '{me}'")

    # Incoming packets print from the event thread while the loop broadcasts.
    esp.espnow.on_receive(
        lambda src, data, rssi: print(f"[{src} {rssi}dBm] {data.decode(errors='replace')}")
    )

    try:
        n = 0
        while True:
            n += 1
            esp.espnow.broadcast(f"{me}: tick {n}".encode())
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        # Free the Wi-Fi driver (~50 KB) before exiting. Over BLE nothing
        # resets the board between sessions, so a driver left resident would
        # leave ~8 KB of heap and cripple every later Bluetooth session.
        esp.espnow.end()
