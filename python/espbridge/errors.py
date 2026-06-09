"""Exception hierarchy for espbridge."""
from __future__ import annotations

from .constants import Status, cmd_name

# Human-readable hints for each firmware status code. These are appended to
# RemoteError messages so failures are actionable without consulting the
# protocol documentation.
_STATUS_HINTS = {
    Status.UNKNOWN_CMD: "firmware too old for this command — reflash the firmware",
    Status.BAD_ARGS: "invalid arguments (pin/bus/length out of range for this chip?)",
    Status.UNSUPPORTED: "not available on this chip or firmware build",
    Status.BUSY: "resource is busy — a previous operation is still running",
    Status.TIMEOUT: "the peripheral/device did not respond in time",
    Status.NO_MEM: "firmware is out of heap — check esp.free_heap(), use smaller "
                   "payloads, or build without BLE (BRIDGE_ENABLE_BLE 0)",
    Status.BAD_PIN: "that pin doesn't exist or can't do this on this chip",
    Status.NOT_INIT: "peripheral not initialized — call its init()/begin first",
    Status.IO: "no ACK on the wire — check wiring, power, device address and "
               "pull-ups (i2c.scan() shows who's answering)",
    Status.WIFI: "Wi-Fi operation failed — check credentials/radio state",
    Status.SOCKET: "socket failed — host unreachable or handle already closed",
    Status.CRC: "payload integrity check failed",
    Status.DENIED: "wireless link not authenticated (SYS_AUTH/password)",
    Status.NOT_FOUND: "no such key/path/handle",
}


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
    """The bridge rejected the wireless-link password (the one passed to
    EspBridge.begin() in the firmware)."""


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
        hint = _STATUS_HINTS.get(self.status)
        super().__init__(f"{cmd_name(cmd)} failed: {name}"
                         + (f" — {hint}" if hint else ""))


def needs_fw(exc: RemoteError, message: str,
             *, status: Status = Status.UNKNOWN_CMD) -> None:
    """Inside an ``except RemoteError`` block, turn a 'feature missing' status
    into a friendly UnsupportedError; re-raise anything else unchanged::

        except RemoteError as e:
            needs_fw(e, "gpio.status() needs firmware >= 0.3.6 — reflash")
    """
    if exc.status == status:
        raise UnsupportedError(message) from None
    raise exc
