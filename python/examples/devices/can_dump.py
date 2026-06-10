"""CAN bus monitor (candump-style) + a periodic heartbeat frame.

Wire a 3.3 V CAN transceiver (SN65HVD230): TX->GPIO21, RX->GPIO22.
"""
import time

from espbridge import Bridge

with Bridge() as esp:
    esp.can.begin(tx=21, rx=22, bitrate=500_000)
    esp.can.on_message(print)  # CAN 123 [2] 01 02
    n = 0
    try:
        while True:
            esp.can.send(0x7FF, n.to_bytes(2, "big"))
            n = (n + 1) & 0xFFFF
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        esp.can.end()
