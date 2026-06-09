"""luma.oled through the bridge — for projects already built on luma.

Identical to luma on a Raspberry Pi except the serial interface line:
``i2c(port=1, address=0x3C)`` becomes ``LumaI2C(esp.i2c, 0x3C)``.

    uv run luma/oled.py            # sh1106 (default)
    uv run luma/oled.py ssd1306    # true SSD1306

For panel adjustments (contrast, vertical offset, mirroring, clone column
offset) use the native driver instead — espbridge.drivers.oled.OLED has a named
method for each: see ../oled_ssd1306.py.
"""
import sys
import time

from luma.core.render import canvas
from luma.oled.device import sh1106, ssd1306

from espbridge import Bridge
from espbridge.compat.luma import LumaI2C

class sh1106_shifted(sh1106):
    """sh1106 with the panel column start corrected for clone panels.

    luma's sh1106 driver hardcodes RAM column 2 as the start of each page
    write; this clone maps the glass from RAM column 0, so luma's default
    leaves the image 2 px right of where it should be. Rewrite that
    per-page column-start command on the fly.
    """
    COLSTART = 0  # luma default is 2

    def command(self, *cmd):
        if len(cmd) == 3 and 0xB0 <= cmd[0] <= 0xB7 and cmd[1:] == (0x02, 0x10):
            cmd = (cmd[0], 0x00 | (self.COLSTART & 0x0F), 0x10 | (self.COLSTART >> 4))
        super().command(*cmd)


driver = {"ssd1306": ssd1306, "sh1106": sh1106_shifted}[
    sys.argv[1].lower() if len(sys.argv) > 1 else "sh1106"
]

with Bridge() as esp:
    # SSD1306/SH1106 are specced to 400 kHz but clone modules usually run
    # fine at 1 MHz — drop back to 400_000 if you see artifacts.
    esp.i2c.init(sda=21, scl=22, freq=1_000_000)
    device = driver(LumaI2C(esp.i2c, 0x3C), width=128, height=64)
    device.persist = True

    with canvas(device) as draw:
        draw.text((0, 10), "luma.oled via espbridge", fill="white")
        draw.rectangle((0, 30, 127, 50), outline="white")
    time.sleep(5)
