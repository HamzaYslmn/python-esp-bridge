"""Scan Wi-Fi networks through the ESP32 radio."""
from espbridge import Bridge

# ble=False: scanning powers up the Wi-Fi radio, which the classic ESP32 can't
# do while a Bluetooth link is live — so run this one over the cable.
with Bridge(ble=False) as esp:
    print("scanning ...")
    for net in esp.wifi.scan():
        print(f"{net.rssi:4d} dBm  ch{net.channel:2d}  {net.auth:13s}  {net.ssid}")
