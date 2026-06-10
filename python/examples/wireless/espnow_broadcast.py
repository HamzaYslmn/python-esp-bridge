"""ESP-NOW auto-mesh, fully wireless — Bluetooth to the boards, ESP-NOW between them.

Scans for every board advertising the bridge service, connects to each over
Bluetooth (no USB cables; default password "espbridge"), and lets them find
each other and talk — discovery by broadcast beacon, data by ACKed unicast:

    uv run espnow_broadcast.py                # find and run every board
    uv run espnow_broadcast.py relays spare2  # only these (name or MAC)

Why two kinds of packets: while Wi-Fi is idle, the coexistence arbiter gives
BLE most of the radio, and broadcasts are single-shot (never ACKed) — bursts
of them vanish into BLE timeslots. So broadcasts carry only the repeating
"HI" beacons (loss-tolerant), and the actual messages go as unicast, which
the MAC layer retries until the peer ACKs. A board never hears its own
broadcasts; boards on other PCs running this script join in automatically.
"""
import sys
import time

from espbridge import Bridge, find_ble_devices

picks = sys.argv[1:] or [d.address for d in find_ble_devices()]
if not picks:
    sys.exit("no boards advertising over Bluetooth")

boards = []
try:
    for sel in picks:
        esp = Bridge(ble=sel, password="espbridge")
        mac = esp.espnow.begin()
        me = esp.info.name or mac
        boards.append({"esp": esp, "me": me, "peers": set()})
        print(f"{me}: Bluetooth link up, ESP-NOW on {mac}")
    if len(boards) == 1:
        print("one board found — beaconing anyway; any board in range "
              "running this script will pair up with it")

    n = 0
    while True:
        n += 1
        for b in boards:
            esp, me = b["esp"], b["me"]
            while esp.espnow.available():
                src, data, rssi = esp.espnow.read()
                if data.startswith(b"HI "):
                    if src not in b["peers"]:
                        esp.espnow.add_peer(src)
                        b["peers"].add(src)
                        print(f"{me} discovered {data[3:].decode(errors='replace')} ({src})")
                else:
                    print(f"{me} heard [{src} {rssi}dBm] {data.decode(errors='replace')}")
        for b in boards:
            esp, me = b["esp"], b["me"]
            esp.espnow.broadcast(b"HI " + me.encode())
            msg = f"{me}: tick {n}".encode()
            for peer in b["peers"]:
                if not esp.espnow.send(peer, msg) and not esp.espnow.send(peer, msg):
                    print(f"{me} -> {peer}: tick {n} undelivered")
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    # Free each board's Wi-Fi driver (~50 KB) before exiting. Over BLE nothing
    # resets the board between sessions, so a driver left resident would leave
    # ~8 KB of heap and cripple every later Bluetooth session.
    for b in boards:
        b["esp"].espnow.end()
        b["esp"].close()
