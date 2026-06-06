"""Persistent boot/run counter in the ESP32's NVS flash."""
from espbridge import Bridge

with Bridge() as esp:
    runs = (esp.nvs.get_int("runs") or 0) + 1
    esp.nvs.set("runs", runs)
    print(f"this script has touched this board {runs} time(s)")
    print(f"stored keys: {esp.nvs.keys()}")
