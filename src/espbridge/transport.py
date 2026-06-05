"""Serial transport with ESP32 auto-detection, plus an in-memory mock for tests."""
from __future__ import annotations

import time
from dataclasses import dataclass

from .constants import KNOWN_USB_IDS
from .errors import NoDeviceError


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

    def __init__(self, port: str, baud: int = 115200, usb_chip: str | None = None):
        import serial

        self.usb_chip = usb_chip
        self.ser = serial.Serial(port, baudrate=baud, timeout=0.05, write_timeout=2.0)

    def read(self) -> bytes:
        data = self.ser.read(1)  # blocks up to `timeout`
        waiting = self.ser.in_waiting
        if data and waiting:
            data += self.ser.read(waiting)
        return data

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


class MockTransport:
    """In-memory transport for hardware-free tests.

    `responder(data: bytes)` is called with everything the host writes; it can
    push firmware->host bytes back via `inject()`.
    """

    def __init__(self, responder=None):
        import queue

        self.usb_chip = None
        self._rx: queue.Queue[bytes] = queue.Queue()
        self.responder = responder
        self.closed = False
        self.baud = 115200

    def inject(self, data: bytes) -> None:
        self._rx.put(data)

    def read(self) -> bytes:
        import queue

        try:
            return self._rx.get(timeout=0.05)
        except queue.Empty:
            return b""

    def write(self, data: bytes) -> None:
        if self.responder is not None:
            self.responder(data)

    def set_baudrate(self, baud: int) -> None:
        self.baud = baud

    def pulse_reset(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
