"""Join Wi-Fi and fetch a URL — the TCP connection runs over the ESP32 radio,
not your computer's network. Handy to give a Pi/PC connectivity through the
ESP32, or to reach devices on the ESP32's Wi-Fi segment.
"""
import sys

from espbridge import Bridge

SSID = sys.argv[1] if len(sys.argv) > 1 else "your-ssid"
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else "your-password"

# ble=False: on the classic ESP32 the Wi-Fi radio can't come up over a live
# Bluetooth session (the firmware refuses with NO_MEM), so use the cable.
with Bridge(ble=False) as esp:
    print(f"joining {SSID!r} ...")
    st = esp.wifi.connect(SSID, PASSWORD)
    print(f"connected: ip={st.ip} rssi={st.rssi} dBm")

    status, body = esp.net.http_get("http://httpbin.io/")
    print(f"HTTP {status}, {len(body)} bytes")
    print(body[:200].decode(errors="replace"))

    # Raw sockets work too:
    with esp.net.tcp_connect("httpbin.io", 80) as sock:
        sock.send(b"HEAD / HTTP/1.0\r\nHost: httpbin.io\r\n\r\n")
        print(sock.recv(timeout=10).decode(errors="replace").splitlines()[0])

    esp.wifi.disconnect()    # powers the radio off, reclaiming ~50 KB of heap
