"""Serial (USB) transport with ESP32 auto-detection."""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..constants import KNOWN_USB_IDS
from ..errors import NoDeviceError


@dataclass(frozen=True)
class PortInfo:
    device: str          # e.g. "COM5" or "/dev/ttyUSB0"
    usb_chip: str | None  # "cp210x" | "ch340" | "ch9102" | "native" | None
    description: str = ""


def find_ports() -> list[PortInfo]:
    """List serial ports that look like an ESP32 (by USB VID/PID)."""
    from serial.tools import list_ports

    found: list[PortInfo] = []
    for p in list_ports.comports():
        if p.vid is None:
            continue
        for (vid, pid), chip in KNOWN_USB_IDS.items():
            if p.vid == vid and (pid is None or p.pid == pid):
                found.append(PortInfo(p.device, chip, p.description or ""))
                break
    return found


def autodetect_port() -> PortInfo:
    ports = find_ports()
    if not ports:
        raise NoDeviceError(
            "no ESP32 serial port found (CP210x/CH340/CH9102/native USB); "
            "pass port='COM5' / '/dev/ttyUSB0' explicitly"
        )
    if len(ports) > 1:
        names = ", ".join(p.device for p in ports)
        raise NoDeviceError(f"multiple ESP32-like ports found ({names}); pass port= explicitly")
    return ports[0]


class SerialTransport:
    """Thin pyserial wrapper. read() returns whatever bytes are available."""

    has_baud = True
    needs_auth = False  # USB implies physical access
    # The firmware UART RX ring is SERIAL_RX_BUF (8704) and drains only as
    # fast as commands execute — a pipelined burst outruns a slow handler
    # (80 ms I2C scan, 1 KB I2C write ~23 ms) and overflows the ring, dropping
    # bytes. Both caps below hold total unacknowledged bytes under the ring
    # minus one max-size frame, so that can't happen.
    # Fire-and-forget bytes before Bridge.send() inserts a ping fence:
    burst_window = 6400
    # Waited-request bytes in flight (Bridge._reserve_window blocks past this).
    # Three max-size frames fit, which saturates the full-duplex line: a spare
    # frame is always queued to transmit while a reply streams back.
    max_inflight = 6400

    def __init__(self, port: str, baud: int = 115200, usb_chip: str | None = None):
        import serial

        self.usb_chip = usb_chip
        self.ser = serial.Serial(port, baudrate=baud, timeout=0.05, write_timeout=2.0)

    def read(self) -> bytes:
        # One syscall: drain whatever's buffered, else block up to `timeout` for 1 byte.
        return self.ser.read(self.ser.in_waiting or 1)

    def write(self, data: bytes) -> None:
        self.ser.write(data)

    def set_baudrate(self, baud: int) -> None:
        self.ser.baudrate = baud
        self.ser.reset_input_buffer()

    def pulse_reset(self) -> None:
        """Toggle RTS/DTR the way esptool does to hard-reset the ESP32."""
        self.ser.dtr = False
        self.ser.rts = True
        time.sleep(0.1)
        self.ser.rts = False

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass
