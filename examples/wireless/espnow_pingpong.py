"""Two boards, one machine, zero cables: ESP-NOW ping/pong over two Bluetooth links.

Discovers the first two bridges advertising over Bluetooth, connects to both,
then has board A ESP-NOW-ping board B, which answers "pong":

    host ──BLE──> board A ──ESP-NOW──> board B ──BLE──> host
                        <──ESP-NOW pong──

    uv run espnow_pingpong.py

The responder replies from inside the RX callback, which runs on that
bridge's reader thread — so it must use send(..., wait=False) (fire-and-
forget): a blocking send would wait for a reply that the same thread is
supposed to process.
"""
import logging
import sys
import time

from espbridge import Bridge, find_ble_devices

logging.basicConfig(level=logging.INFO, format="%(message)s")

print("scanning for bridges over Bluetooth ...")
devs = find_ble_devices()
if len(devs) < 2:
    sys.exit(f"need two advertising bridges, found {len(devs)}")
sel_a, sel_b = devs[0], devs[1]
print(f"pinger:  {sel_a.name}")
print(f"ponger:  {sel_b.name}")

with Bridge(ble=sel_a.mac, password="espbridge") as a, \
     Bridge(ble=sel_b.mac, password="espbridge") as b:
    mac_a = a.espnow.begin()
    mac_b = b.espnow.begin()
    a.espnow.add_peer(mac_b)
    b.espnow.add_peer(mac_a)
    print(f"A = {mac_a}   free heap {a.free_heap()['free']} B")
    print(f"B = {mac_b}   free heap {b.free_heap()['free']} B")

    # B: answer every "ping" with a "pong" (fire-and-forget — see docstring).
    b.espnow.on_receive(
        lambda src, data, rssi: (
            print(f"  B got {data!r} from {src} at {rssi} dBm -> ponging"),
            b.espnow.send(src, b"pong", wait=False),
        ) if data == b"ping" else None
    )

    for n in range(1, 4):
        t0 = time.perf_counter()
        acked = a.espnow.send(mac_b, b"ping")  # True = B's radio ACKed
        src, data, rssi = a.espnow.read(timeout=5.0)
        dt = (time.perf_counter() - t0) * 1000
        print(f"#{n}: ping acked={acked}, got {data!r} from {src} "
              f"at {rssi} dBm in {dt:.0f} ms")
        time.sleep(0.5)

    print("ping/pong over ESP-NOW while both boards stay on the BLE link — coex works.")
