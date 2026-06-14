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

Optional flow-control attributes (a transport sets them to opt in; absent ==
unbounded, the serial default):
    max_inflight: int      # cap on request bytes written but not yet replied —
                           # Bridge blocks writers past it so pipelined traffic
                           # can't overflow the firmware's link RX buffer (BLE)
    burst_window: int      # same cap for fire-and-forget send(); past it send()
                           # inserts a ping fence to drain the in-flight window
"""
from .serial import PortInfo, SerialTransport, autodetect_port, find_ports
from .mock import MockTransport
from .socket import SocketTransport

__all__ = [
    "PortInfo",
    "SerialTransport",
    "MockTransport",
    "SocketTransport",
    "autodetect_port",
    "find_ports",
]
