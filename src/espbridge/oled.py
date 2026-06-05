"""Direct OLED support: SSD1306 / SH1106 / clones over the bridge's I2C.

    pip install "python-esp-bridge[oled]"   (just adds Pillow for drawing)

    from espbridge import Bridge
    from espbridge.oled import OLED

    with Bridge() as esp:
        oled = OLED(esp)                       # bus init + auto-detect + power up
        with oled.draw() as d:                 # d is a PIL ImageDraw
            d.text((0, 10), "Hello!", fill="white")
            d.rectangle((0, 56, 127, 62), fill="white")

Drawing is plain PIL: text with any TTF font, shapes, bitmaps — build your own
Image and call ``oled.show(image)`` if you prefer.

Compatibility (learned the hard way): cheap "SSD1306" modules are very often
SH1106-family clones. SH1106 lacks the SSD1306's horizontal addressing mode
(bulk framebuffer dumps come out interleaved/wrapped), and clones disagree on
where the visible window sits in controller RAM (column offset 0 vs 2).
This driver therefore:

- writes in *page mode*, which every chip in the family implements,
- powers up with both families' commands (each chip ignores the other's),
- defaults to column offset 0 (true SSD1306s and most 0.96" clones).

Only genuine 1.3" SH1106 modules (window centered in 132-column RAM) need
``OLED(esp, colstart=2)`` — symptom: image shifted sideways with a stripe of
stale pixels at one edge; try colstart 0..4.

Every common panel adjustment has a named method — no datasheet bytes needed:
``contrast(0..255)``, ``invert()``, ``power(False)``, ``vertical_offset(rows)``,
``start_line(row)``, ``flip(horizontal=..., vertical=...)`` and the
``colstart`` attribute.
"""
from __future__ import annotations

import contextlib

from .errors import NoDeviceError

_CMD = 0x00   # control byte: command stream follows
_DATA = 0x40  # control byte: display data follows

_INSTALL_HINT = (
    'OLED drawing needs Pillow — install it with:\n'
    '    pip install "python-esp-bridge[oled]"  (or: pip install pillow)'
)

# PIL packs mode-"1" rows MSB-first; panel pages want the top pixel in bit 0.
_BITREV = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


class OLED:
    """A 128x64/128x32 I2C OLED driven directly over the bridge."""

    def __init__(self, esp, *, addr: int | None = None, sda: int = 21,
                 scl: int = 22, freq: int = 400_000, bus: int = 0,
                 width: int = 128, height: int = 64, colstart: int = 0,
                 contrast: int = 0xCF, init_bus: bool = True):
        try:
            from PIL import Image, ImageDraw
        except ImportError as e:
            raise ImportError(_INSTALL_HINT) from e
        self._Image, self._ImageDraw = Image, ImageDraw

        self._i2c = esp.i2c
        self._bus = bus
        # largest data write minus the control byte (128 on old firmware,
        # whose Wire TX buffer silently truncates longer transmissions)
        self._chunk = getattr(esp.i2c, "max_write", 2046) - 1
        self.width, self.height = width, height
        self.colstart = colstart

        if init_bus:
            self._i2c.init(sda=sda, scl=scl, freq=freq, bus=bus)
        if addr is None:
            found = self._i2c.scan(bus)
            addr = next((a for a in (0x3C, 0x3D) if a in found), None)
            if addr is None:
                raise NoDeviceError(
                    f"no OLED at 0x3C/0x3D (i2c scan found: {[hex(a) for a in found]})"
                )
        self.addr = addr

        self.command(
            0xAE,               # display off
            0xD5, 0x80,         # clock divide
            0xA8, height - 1,   # multiplex
            0xD3, 0x00,         # display offset
            0x40,               # start line 0
            0xAD, 0x8B,         # SH1106: DC-DC on        (NOP on SSD1306)
            0x8D, 0x14,         # SSD1306: charge pump on (NOP on SH1106)
            0xA1, 0xC8,         # flip horizontally + vertically (rotation 0)
            0xDA, 0x12 if height == 64 else 0x02,  # COM pins
            0x81, contrast,
            0xD9, 0xF1,         # precharge
            0xDB, 0x40,         # VCOM detect
            0xA4, 0xA6,         # from RAM, not inverted
        )
        self.clear()            # wipe power-on RAM noise before lighting up
        self.command(0xAF)      # display on

    # ---- low level ------------------------------------------------------------

    def command(self, *cmds: int, wait: bool = True) -> None:
        self._i2c.write(self.addr, bytes([_CMD, *cmds]), self._bus, wait=wait)

    def _write_data(self, data: bytes, *, wait: bool = True) -> None:
        chunk = self._chunk
        for off in range(0, len(data), chunk):
            self._i2c.write(self.addr, bytes([_DATA]) + data[off : off + chunk],
                            self._bus,
                            wait=wait and off + chunk >= len(data))

    # ---- drawing ---------------------------------------------------------------

    def show(self, image=None) -> None:
        """Push a PIL image (mode '1' or anything convertible) to the panel."""
        if image is None:
            image = self._Image.new("1", (self.width, self.height))
        if image.mode != "1":  # any nonzero pixel lights up (no dithering)
            image = image.convert("L").point(lambda v: 255 if v else 0, mode="1")
        # Transposing makes each image row a display column, so tobytes()
        # yields the 8-pixel vertical slices pages are made of (MSB-first;
        # the panel wants the top pixel in bit 0, hence the bit reversal).
        raw = image.transpose(self._Image.Transpose.TRANSPOSE).tobytes()
        bpr = self.height // 8  # transposed row = bpr bytes, one per page
        low = 0x00 | (self.colstart & 0x0F)
        high = 0x10 | (self.colstart >> 4)
        pages = self.height // 8
        for page in range(pages):
            # Pipelined: everything fire-and-forget except the final data
            # write, which acts as the frame sync (firmware runs in order).
            self.command(0xB0 + page, low, high, wait=False)
            self._write_data(raw[page::bpr].translate(_BITREV),
                             wait=page == pages - 1)

    @contextlib.contextmanager
    def draw(self):
        """Context manager: yields a PIL ImageDraw; pushes the frame on exit."""
        image = self._Image.new("1", (self.width, self.height))
        yield self._ImageDraw.Draw(image)
        self.show(image)

    def clear(self) -> None:
        self.show(None)

    # ---- panel controls -----------------------------------------------------------
    # Named knobs for every common panel adjustment — no datasheet bytes needed.
    # (`colstart` is a plain attribute: `oled.colstart = 2` applies on next show().)

    def contrast(self, value: int) -> None:
        """Brightness, 0..255."""
        self.command(0x81, value & 0xFF)

    def invert(self, enabled: bool = True) -> None:
        """White-on-black <-> black-on-white, instantly (no redraw needed)."""
        self.command(0xA7 if enabled else 0xA6)

    def power(self, on: bool = True) -> None:
        """Panel on/off (RAM contents are kept while off)."""
        self.command(0xAF if on else 0xAE)

    def vertical_offset(self, rows: int = 0) -> None:
        """Shift the image down by 0..63 rows — fixes panels whose glass is
        wired with a vertical offset (image starts a couple rows low/high)."""
        self.command(0xD3, rows & 0x3F)

    def start_line(self, line: int = 0) -> None:
        """Hardware vertical scroll: which RAM row appears at the top."""
        self.command(0x40 | (line & 0x3F))

    def flip(self, *, horizontal: bool = False, vertical: bool = False) -> None:
        """Mirror the panel in hardware (for displays mounted rotated).

        flip(horizontal=True, vertical=True) is a 180° rotation. The vertical
        mirror applies instantly; the horizontal one applies from the next
        show() (it remaps how RAM is written).
        """
        self.command(0xA0 if horizontal else 0xA1,
                     0xC0 if vertical else 0xC8)
