"""espbridge — control every ESP32 peripheral from Python over USB or Bluetooth.

Flash firmware/firmware.ino once, then:

    from espbridge import Bridge

    with Bridge() as esp:                # auto-detects the serial port
        esp.gpio.mode(2, "output")
        esp.gpio.write(2, 1)
        print(esp.adc.read(34))
        print(esp.i2c.scan())
        esp.wifi.connect("ssid", "password")
        sock = esp.net.tcp_connect("example.com", 80)  # TCP through the ESP32 radio

        esp.nvs.set("runs", 1)           # on-board key/value storage
        esp.fs.mount("littlefs")         # on-board files
        esp.can.begin(tx=21, rx=22)      # CAN bus
        esp.ota.flash("new.bin")         # update the firmware over the link

    # device drivers in pure Python (RMT / 1-Wire primitives):
    #   espbridge.neopixel, .dht, .ds18b20, .hcsr04, .ir, .stepper
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

__version__ = "0.3.1"

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
