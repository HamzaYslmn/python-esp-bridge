"""espbridge — control every ESP32 peripheral from Python over USB serial.

Flash esp/esp.ino once, then:

    from espbridge import Bridge

    with Bridge() as esp:                # auto-detects the serial port
        esp.gpio.mode(2, "output")
        esp.gpio.write(2, 1)
        print(esp.adc.read(34))
        print(esp.i2c.scan())
        esp.wifi.connect("ssid", "password")
        sock = esp.net.tcp_connect("example.com", 80)  # TCP through the ESP32 radio
"""
from .bridge import Bridge, BridgeSet, Info, connect_all
from .constants import Cap, ChipModel, Status
from .errors import (
    BridgeError,
    BridgeTimeoutError,
    NoDeviceError,
    ProtocolError,
    RemoteError,
    UnsupportedError,
)
from .transport import find_ports

__version__ = "0.0.2"

__all__ = [
    "Bridge",
    "BridgeSet",
    "connect_all",
    "Info",
    "Cap",
    "ChipModel",
    "Status",
    "BridgeError",
    "BridgeTimeoutError",
    "NoDeviceError",
    "ProtocolError",
    "RemoteError",
    "UnsupportedError",
    "find_ports",
    "__version__",
]
