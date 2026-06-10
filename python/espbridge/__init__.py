"""espbridge — control every ESP32 peripheral from Python over USB or Bluetooth.

Flash the firmware once (Arduino library "python esp bridge"), then:

    from espbridge import Bridge

    with Bridge() as esp:                # Bluetooth first, then USB serial
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

    # device drivers in pure Python (RMT / 1-Wire / I2C primitives), all under
    # espbridge.drivers: dht, oled, neopixel, ds18b20, hcsr04, ir, stepper, ...
    # ...or write/register your own — esp.<name>(...) — see espbridge.drivers
    #    and docs/DRIVERS.md ("bring your own driver").
"""
from .bridge import Bridge, BridgeSet, Info, connect_all
from .constants import Cap, ChipModel, Status
from .drivers import driver_names, register_driver
from .manager import BridgeManager, connect, disconnect_all, shared_manager
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
from .gpio import EdgeEvent
from .watch import WatchEvent


def find_ble_devices(timeout: float = 5.0):
    """Scan for bridges advertising over Bluetooth (needs the [ble] extra)."""
    from .transports.ble import find_ble_devices as _scan

    return _scan(timeout)


def __getattr__(name):
    # Lazy: importing AsyncBridge pulls in asyncio, so defer it until first use
    # rather than charging every sync/USB-only script for the import.
    if name == "AsyncBridge":
        from .aio import AsyncBridge

        return AsyncBridge
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__version__ = "0.11.3"

__all__ = [
    "Bridge",
    "AsyncBridge",
    "BridgeSet",
    "BridgeManager",
    "connect",
    "shared_manager",
    "disconnect_all",
    "connect_all",
    "register_driver",
    "driver_names",
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
    "EdgeEvent",
    "WatchEvent",
    "__version__",
]
