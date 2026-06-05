"""BLE scan through the ESP32: list nearby advertisers for 10 seconds."""
from espbridge import Bridge

with Bridge() as esp:
    print("scanning BLE for 10 s ...")
    for adv in esp.ble.scan(10):
        name = adv.name or "-"
        print(f"{adv.rssi:4d} dBm  {adv.addr}  {name}")
