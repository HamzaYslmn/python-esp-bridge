"""espbridge.drivers.sx127x.SX127x: SX1276 LoRa transceiver over SPI (hardware-free)."""
import pytest

from espbridge.drivers.sx127x import SX127x

# Register addresses mirrored from the driver, for asserting on recorded traffic.
REG_OP_MODE = 0x01
REG_FRF_MSB, REG_FRF_MID, REG_FRF_LSB = 0x06, 0x07, 0x08
REG_FIFO = 0x00
REG_FIFO_ADDR_PTR = 0x0D
REG_IRQ_FLAGS = 0x12
REG_RX_NB_BYTES = 0x13
REG_MODEM_CONFIG_2 = 0x1E
REG_PAYLOAD_LENGTH = 0x22
REG_VERSION = 0x42
REG_FIFO_RX_CURRENT_ADDR = 0x10

LORA = 0x80


class FakeSpi:
    """Records every (tx, cs) frame and returns canned read data.

    For a read frame [reg & 0x7F, 0x00] the second clocked-in byte comes from
    ``regs`` (defaulting to 0). RegVersion always reads back 0x12 (SX1276).
    A small queue lets a test feed successive reads of the same register
    (e.g. IrqFlags: first poll 0x00, then TxDone/RxDone set).
    """

    def __init__(self):
        self.inited = None
        self.frames = []            # list of (tx_bytes, cs)
        self.regs = {REG_VERSION: 0x12}
        self.queues = {}            # reg -> list of values consumed FIFO

    def init(self, *, sck=18, miso=19, mosi=23, freq=1_000_000, mode=0,
             msb_first=True, host=0):
        self.inited = dict(sck=sck, miso=miso, mosi=mosi, freq=freq, mode=mode)

    def transfer(self, tx, *, cs=None, host=0):
        tx = bytes(tx)
        self.frames.append((tx, cs))
        reg = tx[0]
        if reg & 0x80:              # write frame: address has bit 7 set
            return bytes(len(tx))
        addr = reg & 0x7F          # read frame
        q = self.queues.get(addr)
        if q:
            val = q.pop(0)
        else:
            val = self.regs.get(addr, 0)
        return bytes([0x00, val])

    # --- helpers for assertions ---
    def writes(self):
        """All write frames as (reg, value) pairs."""
        return [(tx[0] & 0x7F, tx[1]) for tx, _ in self.frames if tx[0] & 0x80]

    def last_write(self, reg):
        vals = [v for r, v in self.writes() if r == reg]
        return vals[-1] if vals else None


class FakeGpio:
    def __init__(self):
        self.modes = {}
        self.writes = []

    def mode(self, pin, mode):
        self.modes[pin] = mode

    def write(self, pin, value, *, verify=False):
        self.writes.append((pin, value))
        return value

    def read(self, pin):
        return 0


class FakeEsp:
    def __init__(self):
        self.spi = FakeSpi()
        self.gpio = FakeGpio()


def make(esp=None, **kw):
    esp = esp or FakeEsp()
    lora = SX127x(esp, cs=5, **kw)
    return esp, lora


def test_construct_verifies_version_and_inits_spi():
    esp, _ = make()
    assert esp.spi.inited == dict(sck=18, miso=19, mosi=23, freq=1_000_000, mode=0)
    # The version register was read on a held-CS 2-byte frame.
    assert (bytes([REG_VERSION, 0x00]), 5) in esp.spi.frames


def test_bad_version_raises():
    esp = FakeEsp()
    esp.spi.regs[REG_VERSION] = 0x11      # not an SX1276
    with pytest.raises(RuntimeError):
        SX127x(esp, cs=5)


def test_enters_lora_sleep_then_standby():
    esp, _ = make()
    opmodes = [v for r, v in esp.spi.writes() if r == REG_OP_MODE]
    # First two OpMode writes select LoRa SLEEP (0x80) then LoRa STANDBY (0x81).
    assert opmodes[0] == LORA | 0x00
    assert opmodes[1] == LORA | 0x01
    # Every OpMode write keeps the LongRangeMode bit set.
    assert all(v & LORA for v in opmodes)


def test_programs_frf_for_433mhz():
    esp, _ = make(freq=433_000_000)
    # Frf = round(433e6 * 2**19 / 32e6) = 7094272 = 0x6C4000.
    frf = round(433_000_000 * (1 << 19) / 32_000_000)
    assert frf == 0x6C4000
    assert esp.spi.last_write(REG_FRF_MSB) == (frf >> 16) & 0xFF   # 0x6C
    assert esp.spi.last_write(REG_FRF_MID) == (frf >> 8) & 0xFF    # 0x40
    assert esp.spi.last_write(REG_FRF_LSB) == frf & 0xFF           # 0x00


def test_sets_sf7_with_crc():
    esp, _ = make(sf=7)
    # RegModemConfig2: SF[7:4]=7, CRC-on bit2 -> (7<<4)|0x04 = 0x74.
    assert esp.spi.last_write(REG_MODEM_CONFIG_2) == 0x74


def test_reset_pulse_low_then_high():
    esp, _ = make(reset=14)
    assert esp.gpio.modes[14] == "output"
    assert esp.gpio.writes[:2] == [(14, 0), (14, 1)]


def test_invalid_args_rejected():
    with pytest.raises(ValueError):
        make(sf=5)
    with pytest.raises(ValueError):
        make(cr=9)
    with pytest.raises(ValueError):
        make(sync_word=0x100)
    with pytest.raises(ValueError):
        make(bw=99_999)


def test_send_writes_fifo_payload_length_and_tx_mode():
    esp, lora = make()
    payload = b"Hi!"
    # IrqFlags poll: first read 0 (not done), then TxDone set.
    esp.spi.queues[REG_IRQ_FLAGS] = [0x00, 0x08]
    lora.send(payload)

    writes = esp.spi.writes()
    # FIFO got each payload byte in order.
    fifo_bytes = bytes(v for r, v in writes if r == REG_FIFO)
    assert fifo_bytes == payload
    assert esp.spi.last_write(REG_PAYLOAD_LENGTH) == len(payload)
    # PayloadLength is written before the TX-mode OpMode write.
    last_opmode = esp.spi.last_write(REG_OP_MODE)
    assert last_opmode == LORA | 0x03          # TX mode
    # TxDone (0x08) was cleared by a write-1.
    assert (REG_IRQ_FLAGS, 0x08) in writes


def test_send_rejects_bad_length():
    esp, lora = make()
    with pytest.raises(ValueError):
        lora.send(b"")
    with pytest.raises(ValueError):
        lora.send(b"x" * 256)


def test_receive_returns_canned_payload():
    esp, lora = make()
    payload = b"ping"
    # First IrqFlags poll: not done; then RxDone set (no CRC error).
    esp.spi.queues[REG_IRQ_FLAGS] = [0x00, 0x40, 0x40]
    esp.spi.regs[REG_RX_NB_BYTES] = len(payload)
    esp.spi.regs[REG_FIFO_RX_CURRENT_ADDR] = 0x00
    esp.spi.queues[REG_FIFO] = list(payload)

    got = lora.receive(timeout=1.0)
    assert got == payload
    # Entered RX-continuous mode.
    opmodes = [v for r, v in esp.spi.writes() if r == REG_OP_MODE]
    assert (LORA | 0x05) in opmodes
    # RxDone was cleared.
    assert (REG_IRQ_FLAGS, 0x40 | 0x20) in esp.spi.writes()


def test_receive_returns_none_on_crc_error():
    esp, lora = make()
    # RxDone with the PayloadCrcError bit (0x20) also set.
    esp.spi.queues[REG_IRQ_FLAGS] = [0x40 | 0x20, 0x40 | 0x20]
    assert lora.receive(timeout=1.0) is None


def test_receive_returns_none_on_timeout():
    esp, lora = make()
    esp.spi.queues[REG_IRQ_FLAGS] = []     # never done -> default 0
    assert lora.receive(timeout=0.05) is None
