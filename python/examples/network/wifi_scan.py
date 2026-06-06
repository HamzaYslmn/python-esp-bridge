"""Scan Wi-Fi networks through the ESP32 radio."""
from espbridge import Bridge

with Bridge() as esp:
    print("scanning ...")
    for net in esp.wifi.scan():
        print(f"{net.rssi:4d} dBm  ch{net.channel:2d}  {net.auth:13s}  {net.ssid}")
