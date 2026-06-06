"""Transports: byte links the Bridge protocol runs over (serial, BLE, mock).

A transport provides:
    read() -> bytes        # whatever is available (may block briefly, b"" ok)
    write(data: bytes)     # send raw bytes
    close()
    set_baudrate(baud)     # serial links only; no-op elsewhere
    pulse_reset()          # serial links only; no-op elsewhere
    usb_chip: str | None   # adapter hint for baud upgrade ("cp210x", ...)
    has_baud: bool         # False to skip baud negotiation entirely
    needs_auth: bool       # True = Bridge sends SYS_AUTH before the handshake
"""
from .serial import PortInfo, SerialTransport, autodetect_port, find_ports
from .mock import MockTransport

__all__ = [
    "PortInfo",
    "SerialTransport",
    "MockTransport",
    "autodetect_port",
    "find_ports",
]
