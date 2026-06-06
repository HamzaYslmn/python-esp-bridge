"""Two-board ESP-NOW chat, fully wireless — Bluetooth to the board, ESP-NOW between boards.

No USB cable, no router: each machine talks to its ESP32 over the Bluetooth
link (default password "espbridge", set via EspBridge.begin()), and the
boards talk to each other over ESP-NOW (~250 bytes per packet, delivery ACKs).

Run on each machine:

    uv run espnow_pair.py                          # prints this board's MAC, listens
    uv run espnow_pair.py a0:b1:c2:d3:e4:f5        # chat with the MAC the other side printed
    uv run espnow_pair.py a0:b1:c2:... relays      # pick which bridge by name (or MAC)

send() returns True when the peer's radio acknowledged the packet — instant
delivery feedback without any connection setup.
"""
import sys
import time

from espbridge import Bridge

args = sys.argv[1:]
peer = args[0] if args else None
board = args[1] if len(args) > 1 else True

with Bridge(ble=board, password="espbridge") as esp:
    mac = esp.espnow.begin()
    print(f"connected over Bluetooth: {esp.info.name or esp.info.mac}")
    print(f"this board's ESP-NOW MAC: {mac}  (pass it to the other side)")

    esp.espnow.on_receive(
        lambda src, data, rssi: print(f"\r[{src} {rssi}dBm] {data.decode(errors='replace')}\n> ", end="")
    )

    if peer is None:
        print("listening — re-run with the peer's MAC to also send. Ctrl+C to quit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            sys.exit(0)

    esp.espnow.add_peer(peer)
    print(f"chatting with {peer} — type a line and press Enter. Ctrl+C to quit.")
    try:
        while True:
            line = input("> ")
            if not line:
                continue
            ok = esp.espnow.send(peer, line.encode())
            if not ok:
                print("  (no ACK — peer offline or on another channel?)")
    except KeyboardInterrupt:
        pass
