"""espbridge.drivers.oled.OLED: dual-chip init and page-mode framing (SSD1306 + SH1106)."""
import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from espbridge.drivers.oled import OLED  # noqa: E402


class FakeI2c:
    def __init__(self):
        self.inited = None
        self.writes = []

    def init(self, *, sda=21, scl=22, freq=400_000, bus=0):
        self.inited = (sda, scl, freq, bus)

    def scan(self, bus=0):
        return [0x3C]

    def write(self, addr, data, bus=0, wait=True):
        # I2C_WRITE carries at most MAX_PAYLOAD - 2 = 2046 bytes.
        # The firmware sizes the Wire TX buffer to match, so this limit is hard.
        assert len(data) <= 2046, "exceeds I2C_WRITE limit"
        self.writes.append((addr, bytes(data)))


class FakeEsp:
    def __init__(self):
        self.i2c = FakeI2c()


def _cmd_stream(writes):
    return b"".join(w[1][1:] for w in writes if w[1][0] == 0x00)


def _data_pages(writes):
    """Reassemble the pixel bytes for each display page from the raw I2C write log.

    Each page may arrive in multiple chunks, and every chunk is preceded by a
    page + column addressing command (0xB0..0xB7).  This function groups the data
    writes by page number and concatenates them in order.
    """
    pages: dict[int, bytearray] = {}
    cur = None
    for _, d in writes:
        if d[0] == 0x00 and len(d) >= 2 and 0xB0 <= d[1] <= 0xB7:
            cur = d[1] - 0xB0
            pages.setdefault(cur, bytearray())
        elif d[0] == 0x40 and cur is not None:
            pages[cur] += d[1:]
    return [bytes(pages[k]) for k in sorted(pages)]


def test_init_powers_both_chip_families_and_clears():
    esp = FakeEsp()
    OLED(esp)
    assert esp.i2c.inited == (21, 22, 400_000, 0)
    assert all(w[0] == 0x3C for w in esp.i2c.writes)
    cmds = _cmd_stream(esp.i2c.writes)
    assert bytes([0xAD, 0x8B]) in cmds   # SH1106 internal DC-DC on (harmless NOP on SSD1306)
    assert bytes([0x8D, 0x14]) in cmds   # SSD1306 charge-pump enable (harmless NOP on SH1106)
    assert cmds.endswith(b"\xaf")        # 0xAF = display on — must come last, after RAM is cleared
    pages = _data_pages(esp.i2c.writes)
    assert len(pages) == 8 and all(set(p) == {0} for p in pages)  # all 8 pages must be zeroed


def test_show_writes_pages_at_colstart_zero():
    esp = FakeEsp()
    oled = OLED(esp)
    esp.i2c.writes.clear()

    img = Image.new("1", (128, 64))
    img.putpixel((0, 0), 1)     # page 0, column 0, bit 0
    img.putpixel((5, 9), 1)     # page 1, column 5, bit 1
    img.putpixel((127, 63), 1)  # page 7, column 127, bit 7
    oled.show(img)

    cmds = [w[1] for w in esp.i2c.writes if w[1][0] == 0x00]
    assert [c[1] for c in cmds] == [0xB0 + p for p in range(8)]  # pages addressed in order 0–7
    assert all(c[2] == 0x00 and c[3] == 0x10 for c in cmds)      # lower nibble 0x00 + upper nibble 0x10 = column 0

    pages = _data_pages(esp.i2c.writes)
    assert len(pages) == 8 and all(len(p) == 128 for p in pages)
    # In page mode each byte encodes 8 vertical pixels; bit 0 is the topmost row.
    assert pages[0][0] == 0x01   # (0,0) → page 0, col 0, bit 0 → 0b00000001
    assert pages[1][5] == 0x02   # (5,9) → page 1, col 5, bit 1 → 0b00000010
    assert pages[7][127] == 0x80  # (127,63) → page 7, col 127, bit 7 → 0b10000000
    assert sum(byte for p in pages for byte in p) == 0x01 + 0x02 + 0x80  # only the three set pixels are non-zero


def test_colstart_two_for_genuine_sh1106():
    # Many genuine SH1106 modules have a 132-column driver mapped to a 128-pixel
    # display, leaving a 2-column offset.  colstart=2 shifts addressing accordingly.
    esp = FakeEsp()
    oled = OLED(esp, colstart=2)
    esp.i2c.writes.clear()
    oled.clear()
    cmds = [w[1] for w in esp.i2c.writes if w[1][0] == 0x00]
    assert all(c[2] == 0x02 and c[3] == 0x10 for c in cmds)  # lower nibble = 2, upper nibble = 0x10 → column 2


def test_split_page_writes_readdress_each_chunk():
    """A heap-squeezed firmware Wire buffer (128 B over BLE) can't take a full
    128-byte page per write; every chunk must re-set page + column explicitly
    because clones disagree on column-pointer behavior across transactions."""
    esp = FakeEsp()
    oled = OLED(esp, colstart=2)
    oled._chunk = 125  # what i2c.max_write - 1 yields with a 128 B Wire buffer
    esp.i2c.writes.clear()

    img = Image.new("1", (128, 64))
    img.putpixel((126, 0), 1)  # page 0, column 126 — lands in the 3-byte tail
    oled.show(img)

    cmds = [w[1] for w in esp.i2c.writes if w[1][0] == 0x00]
    datas = [w[1] for w in esp.i2c.writes if w[1][0] == 0x40]
    assert len(cmds) == 16 and len(datas) == 16  # 8 pages × 2 chunks each = 16 command + 16 data writes
    assert all(len(d) - 1 in (125, 3) for d in datas)  # first chunk is 125 bytes, tail is 3 bytes
    # Each chunk re-addresses the column.  colstart=2, so:
    #   chunk 0: column 0+2 → lower nibble 0x02, upper nibble 0x10
    #   chunk 1: column 125+2 = 127 = 0x7F → lower nibble 0x0F, upper nibble 0x17
    assert [(c[2], c[3]) for c in cmds[:2]] == [(0x02, 0x10), (0x0F, 0x17)]
    pages = _data_pages(esp.i2c.writes)
    assert len(pages) == 8 and all(len(p) == 128 for p in pages)
    assert pages[0][126] == 0x01  # the set pixel at column 126 lands in the 3-byte tail chunk


def test_draw_context_manager_pushes_frame():
    esp = FakeEsp()
    oled = OLED(esp)
    esp.i2c.writes.clear()
    with oled.draw() as d:
        d.rectangle((0, 0, 127, 63), fill="white")
    pages = _data_pages(esp.i2c.writes)
    assert all(byte == 0xFF for p in pages for byte in p)  # fully lit


def test_named_panel_controls():
    esp = FakeEsp()
    oled = OLED(esp)
    esp.i2c.writes.clear()

    oled.contrast(0xFF)
    oled.invert()
    oled.vertical_offset(2)
    oled.start_line(8)
    oled.flip(horizontal=True, vertical=True)   # 180 degrees
    oled.power(False)

    sent = [w[1][1:] for w in esp.i2c.writes]
    assert sent == [
        bytes([0x81, 0xFF]),
        bytes([0xA7]),
        bytes([0xD3, 0x02]),
        bytes([0x40 | 0x08]),
        bytes([0xA0, 0xC0]),
        bytes([0xAE]),
    ]


def test_colstart_is_runtime_adjustable():
    esp = FakeEsp()
    oled = OLED(esp)            # colstart 0
    oled.colstart = 2           # runtime adjustment, e.g. after discovering the panel is a 1.3" SH1106
    esp.i2c.writes.clear()
    oled.clear()
    cmds = [w[1] for w in esp.i2c.writes if w[1][0] == 0x00]
    assert all(c[2] == 0x02 and c[3] == 0x10 for c in cmds)
