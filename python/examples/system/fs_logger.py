"""Log readings to the ESP32's internal flash (LittleFS) and read them back.

The data stays on the board across power cycles — useful when the board
runs detached and you collect logs over Bluetooth later.
"""
import time

from espbridge import Bridge

with Bridge() as esp:
    lfs = esp.fs.mount("littlefs")
    print(f"littlefs: {lfs.used_kb}/{lfs.total_kb} KB used")

    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} adc={esp.adc.read(34)}\n"
    lfs.append_file("/sensor.log", line.encode())

    print(lfs.read_file("/sensor.log").decode(), end="")
    for name, size, isdir in lfs.list("/"):
        print(f"{'<dir> ' if isdir else ''}{name}  {size} B")
