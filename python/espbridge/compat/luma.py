"""Adapter that lets the luma.oled / luma.lcd display libraries drive their
displays through the bridge's I2C — so you get the battle-tested drivers
(SSD1306, SH1106, SSD1309, ...) and PIL drawing, while the ESP32 just relays
I2C traffic.

    pip install luma.oled

    from luma.oled.device import ssd1306        # or sh1106, ...
    from espbridge.compat.luma import LumaI2C

    esp.i2c.init(sda=21, scl=22, freq=400_000)
    device = ssd1306(LumaI2C(esp.i2c, addr=0x3C))

This module deliberately does not import luma — it only implements the
``command``/``data``/``cleanup`` serial-interface protocol luma expects.
"""
from __future__ import annotations

from ..constants import MAX_PAYLOAD

_CMD_MODE = 0x00   # control byte: command stream follows
_DATA_MODE = 0x40  # control byte: display data follows


class LumaI2C:
    """luma serial interface backed by an espbridge I2c bus."""

    def __init__(self, i2c, addr: int = 0x3C, bus: int = 0):
        self._i2c = i2c
        self._addr = addr
        self._bus = bus
        # Maximum bytes of display data per I2C write, leaving one byte for
        # the control byte prefix. Old firmware had a 128-byte Wire TX buffer
        # that silently truncated longer writes, so we respect max_write when
        # available and fall back to 2046 (a safe limit on current firmware).
        # Read once here, so construct LumaI2C *after* esp.i2c.init() to pick
        # up the firmware-negotiated buffer size.
        self._chunk = getattr(i2c, "max_write", 2046) - 1

    def command(self, *cmd: int) -> None:
        self._i2c.write(self._addr, bytes([_CMD_MODE, *cmd]), self._bus)

    def data(self, data) -> None:
        data = bytes(data)  # luma may pass a list of ints
        for off in range(0, len(data), self._chunk):
            self._i2c.write(self._addr,
                            bytes([_DATA_MODE]) + data[off : off + self._chunk],
                            self._bus)

    def cleanup(self) -> None:
        pass  # nothing to release; the Bridge owns the link


class LumaSPI:
    """luma serial interface for SPI displays (SSD1306-SPI, ST77xx, ILI9341, ...).

        from luma.lcd.device import st7735
        device = st7735(LumaSPI(esp, dc=4, rst=16, cs=5), width=160, height=128)
    """

    def __init__(self, esp, *, dc: int, rst: int | None = None, cs: int | None = None,
                 sck: int = 18, mosi: int = 23, miso: int = 19,
                 freq: int = 8_000_000, host: int = 0, init_bus: bool = True):
        self._spi = esp.spi
        self._gpio = esp.gpio
        self._dc = dc
        self._cs = cs
        self._host = host
        if init_bus:
            self._spi.init(sck=sck, miso=miso, mosi=mosi, freq=freq, host=host)
        self._gpio.mode(dc, "output")
        if rst is not None:  # power-on reset pulse, as luma's own spi interface does
            import time
            self._gpio.mode(rst, "output")
            self._gpio.write(rst, 0)
            time.sleep(0.01)
            self._gpio.write(rst, 1)
            time.sleep(0.01)

    def _send(self, data: bytes) -> None:
        # Split large payloads into chunks that fit within the bridge's max
        # transfer size. CS is toggled around each chunk, but display
        # controllers latch data per byte, so mid-transfer CS gaps are fine.
        max_chunk = MAX_PAYLOAD - 2  # SPI_TRANSFER payload cap minus header (matches spi._CHUNK)
        for off in range(0, len(data), max_chunk):
            self._spi.transfer(data[off : off + max_chunk], cs=self._cs, host=self._host)

    def command(self, *cmd: int) -> None:
        self._gpio.write(self._dc, 0)
        self._send(bytes(cmd))

    def data(self, data) -> None:
        self._gpio.write(self._dc, 1)
        self._send(bytes(data))

    def cleanup(self) -> None:
        pass  # nothing to release; the Bridge owns the SPI bus and GPIO pins
