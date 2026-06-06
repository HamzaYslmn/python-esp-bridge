"""UDP echo through the ESP32 radio: sends a datagram and waits for a reply.

Start a UDP echo on some machine in the ESP32's network first, e.g.:
    ncat -e /bin/cat -kul 6000        (Linux)
"""
import sys

from espbridge import Bridge

SSID = sys.argv[1] if len(sys.argv) > 1 else "your-ssid"
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else "your-password"
TARGET = (sys.argv[3] if len(sys.argv) > 3 else "192.168.1.10", 6000)

with Bridge() as esp:
    st = esp.wifi.connect(SSID, PASSWORD)
    print(f"connected: ip={st.ip}")
    with esp.net.udp(5005) as sock:
        sock.sendto(b"hello from python-esp-bridge", TARGET)
        data, addr = sock.recvfrom(timeout=5)
        print(f"reply from {addr}: {data!r}")
