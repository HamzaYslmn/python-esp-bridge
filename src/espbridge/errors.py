"""Exception hierarchy for espbridge."""
from __future__ import annotations

from .constants import Status


class BridgeError(Exception):
    """Base class for all espbridge errors."""


class BridgeTimeoutError(BridgeError, TimeoutError):
    """The firmware did not answer in time."""


class ProtocolError(BridgeError):
    """Malformed frame, CRC mismatch, or protocol version mismatch."""


class NoDeviceError(BridgeError):
    """No (or ambiguous) ESP32 bridge serial port found."""


class UnsupportedError(BridgeError):
    """The connected chip lacks this capability (e.g. DAC on ESP32-S3)."""


class AuthError(BridgeError):
    """The bridge rejected the wireless-link password (see BRIDGE_PASSWORD
    at the top of firmware/firmware.ino)."""


class RemoteError(BridgeError):
    """The firmware returned an error status for a request."""

    def __init__(self, status: int, cmd: int):
        try:
            self.status = Status(status)
            name = self.status.name
        except ValueError:
            self.status = status  # unknown code
            name = f"0x{status:02X}"
        self.cmd = cmd
        super().__init__(f"firmware error {name} for command 0x{cmd:04X}")
