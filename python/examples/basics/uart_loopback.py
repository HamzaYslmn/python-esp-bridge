"""Second UART: talk to GPS modules, modems, or any serial device.

Wires a spare UART (port 1, TX=17/RX=16 by default) and proves it with a
loopback — jumper GPIO 17 to GPIO 16 and every byte written comes back:

    uv run basics/uart_loopback.py

For a real device, wire its TX->16 / RX->17, match the baud, and read away —
a GPS at 9600 is `esp.uart.init(baud=9600)` then `port.readline()` per NMEA
sentence. UART RX is pushed from the board as events, so reads don't poll.
"""
from espbridge import Bridge

with Bridge() as esp:
    port = esp.uart.init(port=1, tx=17, rx=16, baud=115_200)

    msg = b"hello, loopback!\r\n"
    port.write(msg)
    back = port.readline(timeout=1.0)

    if back == msg:
        print(f"loopback OK: {back!r}")
    elif back:
        print(f"got {back!r} — something else is talking on the pins")
    else:
        print("nothing received — jumper GPIO 17 to GPIO 16 for the loopback")
    port.close()
