"""I2C master (buses 0 and 1)."""
from __future__ import annotations

import struct

from . import constants as C


class I2c:
    """I2C master on bus 0 or 1.

        esp.i2c.init(sda=21, scl=22)
        esp.i2c.scan()                       # [0x3c, 0x68]
        esp.i2c.write_reg(0x68, 0x6b, 0x00)  # wake an MPU-6050
        esp.i2c.read_reg(0x68, 0x75)         # WHO_AM_I -> b'\\x68'
    """

    def __init__(self, bridge):
        self._b = bridge
        self._max_write: int | None = None
        self.buses: dict[int, dict] = {}  # bus -> {sda, scl, freq}, as configured

    @property
    def max_write(self) -> int:
        """Largest data block write() accepts, firmware-dependent: older
        firmware leaves Wire's TX buffer at its 128-byte default and
        silently truncates anything longer. Firmware >= 0.3.0 reports the
        Wire buffer it could actually allocate in the init() reply (a
        heap-squeezed board may get less than the 2 KB default)."""
        if self._max_write is not None:
            return self._max_write
        info = self._b.info
        if info is not None and info.fw_version < (0, 3, 0):
            return 128
        return C.MAX_PAYLOAD - 2  # 2 bytes of header (bus index + device address) come before the data

    def init(self, *, sda: int = 21, scl: int = 22, freq: int = 400_000, bus: int = 0) -> None:
        """Configure a bus's pins and clock; call once before any transfer."""
        r = self._b.request(C.I2C_INIT, struct.pack(">BBBI", bus, sda, scl, freq))
        if len(r) >= 2:  # firmware >= 0.3.0 replies with the Wire TX buffer size as a u16
            self._max_write = struct.unpack(">H", r[:2])[0] - 2
        self.buses[bus] = {"sda": sda, "scl": scl, "freq": freq}

    def scan(self, bus: int = 0) -> list[int]:
        """Addresses (7-bit) that ACK on the bus."""
        r = self._b.request(C.I2C_SCAN, bytes([bus]), timeout=5.0)
        return list(r[1 : 1 + r[0]])

    def write(self, addr: int, data: bytes, bus: int = 0, *, wait: bool = True) -> None:
        """Write bytes to a device. ``wait=False`` sends fire-and-forget —
        no ACK round-trip, errors are not reported; pair a burst of unwaited
        writes with a final waited one to sync (the firmware executes
        requests in arrival order)."""
        if len(data) > self.max_write:
            raise ValueError(f"max {self.max_write} bytes per I2C write on this firmware "
                             f"(newer firmware raises this to {C.MAX_PAYLOAD - 2} bytes)")
        payload = bytes([bus, addr]) + bytes(data)
        if wait:
            self._b.request(C.I2C_WRITE, payload)
        else:
            self._b.send(C.I2C_WRITE, payload)

    def read(self, addr: int, n: int, bus: int = 0) -> bytes:
        """Read n bytes (1..255) straight from a device."""
        if not 1 <= n <= 255:
            raise ValueError("read length must be 1..255")
        return self._b.request(C.I2C_READ, bytes([bus, addr, n]))

    def write_read(self, addr: int, wdata: bytes, rlen: int, bus: int = 0) -> bytes:
        """Write then read with a repeated start (typical register read)."""
        if len(wdata) > 255 or not 1 <= rlen <= 255:
            raise ValueError("wdata max 255 bytes, rlen 1..255")
        payload = bytes([bus, addr, len(wdata)]) + bytes(wdata) + bytes([rlen])
        return self._b.request(C.I2C_WRITE_READ, payload)

    def read_reg(self, addr: int, reg: int, n: int = 1, bus: int = 0) -> bytes:
        """Read n bytes starting at register `reg` (write reg, repeated start)."""
        return self.write_read(addr, bytes([reg]), n, bus)

    def write_reg(self, addr: int, reg: int, data: bytes | int, bus: int = 0) -> None:
        """Write register `reg` with `data` (a single byte int, or bytes)."""
        data = bytes([data]) if isinstance(data, int) else bytes(data)
        self.write(addr, bytes([reg]) + data, bus)

    def deinit(self, bus: int = 0) -> None:
        """Release the bus and its pins on the firmware."""
        self._b.request(C.I2C_DEINIT, bytes([bus]))
        self.buses.pop(bus, None)

    # House style: begin()/end() work on every peripheral (Arduino-friendly).
    begin = init
    end = deinit


def init_if_pins(i2c, *, bus: int = 0, sda: int | None = None,
                 scl: int | None = None) -> None:
    """Bring up an I2C bus only when pins are given; otherwise do nothing and
    assume a prior ``init()``. The one-liner device drivers use in __init__ so
    several chips can share one already-configured bus (pass ``esp.i2c``)."""
    if sda is None and scl is None:
        return
    pins = {k: v for k, v in (("sda", sda), ("scl", scl)) if v is not None}
    i2c.init(bus=bus, **pins)


def bind_i2c(bridge, address: int, *, bus: int = 0,
             sda: int | None = None, scl: int | None = None):
    """Wire an I2C device driver to a bus; returns ``(i2c, address, bus)``.

    Collapses the identical preamble every bundled I2C driver shares::

        self._i2c, self._addr, self._bus = bind_i2c(bridge, address,
                                                     bus=bus, sda=sda, scl=scl)

    Brings the bus up (``init()``) only when sda/scl are given, otherwise assumes
    a prior ``esp.i2c.init()``."""
    i2c = bridge.i2c
    init_if_pins(i2c, bus=bus, sda=sda, scl=scl)
    return i2c, address, bus
