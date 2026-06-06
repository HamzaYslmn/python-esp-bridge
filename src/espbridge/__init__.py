"""espbridge — control every ESP32 peripheral from Python over USB serial.

Flash firmware/firmware.ino once, then:

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
    AuthError,
    BridgeError,
    BridgeTimeoutError,
    NoDeviceError,
    ProtocolError,
    RemoteError,
    UnsupportedError,
)
from .transports import find_ports


def find_ble_devices(timeout: float = 5.0):
    """Scan for bridges advertising over Bluetooth (needs the [ble] extra)."""
    from .transports.ble import find_ble_devices as _scan

    return _scan(timeout)

__version__ = "0.2.0"

__all__ = [
    "Bridge",
    "BridgeSet",
    "connect_all",
    "find_ble_devices",
    "Info",
    "Cap",
    "ChipModel",
    "Status",
    "AuthError",
    "BridgeError",
    "BridgeTimeoutError",
    "NoDeviceError",
    "ProtocolError",
    "RemoteError",
    "UnsupportedError",
    "find_ports",
    "__version__",
]
