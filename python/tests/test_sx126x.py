"""espbridge.drivers.sx126x.SX126x: command-based LoRa radio, protocol-level.

No hardware: a FakeEsp records every SPI frame (tx bytes + CS pin) and every
GPIO op, with BUSY always reading low (ready). Canned responses are matched by
the command opcode so GetIrqStatus / GetRxBufferStatus / ReadBuffer can drive
the TX and RX flows deterministically.
"""
import pytest

from espbridge.drivers.sx126x import SX126x

# opcodes mirrored from the driver for assertions
SET_STANDBY = 0x80
SET_PACKET_TYPE = 0x8A
SET_RF_FREQUENCY = 0x86
SET_MODULATION = 0x8B
SET_PACKET_PARAMS = 0x8C
WRITE_BUFFER = 0x0E
READ_BUFFER = 0x1E
SET_TX = 0x83
SET_RX = 0x82
GET_IRQ_STATUS = 0x12
GET_RX_BUFFER_STATUS = 0x13


class FakeSpi:
    def __init__(self):
        self.inited = None
        self.frames = []          # list of (tx_bytes, cs)
        self.responses = {}       # opcode -> rx bytes to return (incl. status echo)

    def init(self, *, sck=18, miso=19, mosi=23, freq=1_000_000, mode=0,
             msb_first=True, host=0):
        self.inited = (sck, miso, mosi, freq, mode)

    def transfer(self, tx, *, cs=None, host=0):
        tx = bytes(tx)
        self.frames.append((tx, cs))
        canned = self.responses.get(tx[0])
        if canned is not None:
            # canned is the full MISO frame (byte 0 == status echoed during the
            # opcode byte); pad/truncate to the driver's transfer length.
            return (bytes(canned) + b"\x00" * len(tx))[:len(tx)]
        return b"\x00" * len(tx)


class FakeGpio:
    def __init__(self):
        self.modes = {}
        self.writes = []          # (pin, level) in order
        self.reads = []           # pins read (BUSY polling)

    def mode(self, pin, mode):
        self.modes[pin] = mode

    def write(self, pin, value, *, verify=False):
        v = 1 if value else 0
        self.writes.append((pin, v))
        return v

    def read(self, pin):
        self.reads.append(pin)
        return 0                  # BUSY low == ready


class FakeEsp:
    def __init__(self):
        self.spi = FakeSpi()
        self.gpio = FakeGpio()


CS, RESET, BUSY, DIO1 = 8, 12, 13, 14


def make(**kw):
    esp = FakeEsp()
    radio = SX126x(esp, cs=CS, reset=RESET, busy=BUSY, dio1=DIO1, **kw)
    return esp, radio


def opcodes(esp):
    return [f[0][0] for f in esp.spi.frames]


def first_frame(esp, opcode):
    for tx, cs in esp.spi.frames:
        if tx[0] == opcode:
            return tx, cs
    raise AssertionError(f"opcode {opcode:#04x} never sent")


# --- construction / init -----------------------------------------------------
def test_reset_pulses_reset_pin():
    esp, _ = make()
    # RESET driven low then high during _reset_chip
    assert (RESET, 0) in esp.gpio.writes
    assert (RESET, 1) in esp.gpio.writes
    assert esp.gpio.writes.index((RESET, 0)) < esp.gpio.writes.index((RESET, 1))


def test_init_issues_standby_packettype_freq_modulation():
    esp, _ = make()
    ops = opcodes(esp)
    assert SET_STANDBY in ops
    assert SET_PACKET_TYPE in ops
    assert SET_RF_FREQUENCY in ops
    assert SET_MODULATION in ops
    # ordering: standby before packet type before frequency
    assert ops.index(SET_STANDBY) < ops.index(SET_PACKET_TYPE) < ops.index(SET_RF_FREQUENCY)


def test_packettype_is_lora():
    esp, _ = make()
    tx, _ = first_frame(esp, SET_PACKET_TYPE)
    assert tx[1] == 0x01          # LoRa


def test_rf_frequency_bytes_for_868mhz():
    esp, _ = make(freq=868_000_000)
    tx, _ = first_frame(esp, SET_RF_FREQUENCY)
    reg = round(868_000_000 * (1 << 25) / 32_000_000)
    expect = bytes([(reg >> 24) & 0xFF, (reg >> 16) & 0xFF,
                    (reg >> 8) & 0xFF, reg & 0xFF])
    assert tx[1:5] == expect


def test_modulation_params_sf7_bw125_cr45():
    esp, _ = make(sf=7, bw=125_000, cr=5)
    tx, _ = first_frame(esp, SET_MODULATION)
    assert tx[1] == 7             # SF7
    assert tx[2] == 0x04          # BW 125 kHz
    assert tx[3] == 0x01          # CR 4/5


def test_spi_init_called_with_pins():
    esp, _ = make()
    assert esp.spi.inited == (18, 19, 23, 2_000_000, 0)


def test_cs_used_on_every_command_frame():
    esp, _ = make()
    assert all(cs == CS for _, cs in esp.spi.frames)


def test_busy_polled_before_commands():
    esp, _ = make()
    # BUSY pin read at least once per command frame
    assert esp.gpio.reads.count(BUSY) >= len(esp.spi.frames)


# --- send / TX ---------------------------------------------------------------
def test_send_writes_buffer_then_tx_and_completes_on_txdone():
    esp, radio = make()
    # full MISO frame: 2 echo slots, then Irq[15:8], Irq[7:0]; TxDone = bit 0
    esp.spi.responses[GET_IRQ_STATUS] = bytes([0x00, 0x00, 0x00, 0x01])
    esp.spi.frames.clear()
    esp.gpio.reads.clear()

    radio.send(b"hi!")

    ops = opcodes(esp)
    assert WRITE_BUFFER in ops
    assert SET_TX in ops
    assert ops.index(WRITE_BUFFER) < ops.index(SET_TX)

    wb, _ = first_frame(esp, WRITE_BUFFER)
    assert wb[1] == 0x00          # buffer offset
    assert wb[2:] == b"hi!"       # the payload bytes


def test_send_rejects_empty_and_oversize():
    _, radio = make()
    with pytest.raises(ValueError):
        radio.send(b"")
    with pytest.raises(ValueError):
        radio.send(b"x" * 256)


# --- receive / RX ------------------------------------------------------------
def test_receive_returns_canned_payload():
    esp, radio = make()
    payload = b"world"
    # RxDone = bit 1 (frame: 2 echo slots, Irq[15:8], Irq[7:0])
    esp.spi.responses[GET_IRQ_STATUS] = bytes([0x00, 0x00, 0x00, 0x02])
    # GetRxBufferStatus frame: 2 echo slots, payloadLength, rxStartPointer
    esp.spi.responses[GET_RX_BUFFER_STATUS] = bytes([0x00, 0x00, len(payload), 0x00])
    # ReadBuffer frame: opcode/offset/NOP echo slots, then payload
    esp.spi.responses[READ_BUFFER] = bytes([0x00, 0x00, 0x00]) + payload

    got = radio.receive(timeout=1.0)
    assert got == payload
    ops = opcodes(esp)
    assert SET_RX in ops
    assert GET_RX_BUFFER_STATUS in ops
    assert READ_BUFFER in ops


def test_receive_timeout_returns_none():
    esp, radio = make()
    # IRQ never sets RxDone/Timeout -> host-side timeout fires
    esp.spi.responses[GET_IRQ_STATUS] = bytes([0x00, 0x00, 0x00])
    assert radio.receive(timeout=0.05) is None


def test_receive_crc_error_returns_none():
    esp, radio = make()
    # RxDone (bit1) + CRC error (bit6) set, low byte = 0x42
    esp.spi.responses[GET_IRQ_STATUS] = bytes([0x00, 0x00, 0x00, 0x02 | 0x40])
    assert radio.receive(timeout=1.0) is None


# --- validation --------------------------------------------------------------
def test_bad_args_rejected():
    esp = FakeEsp()
    with pytest.raises(ValueError):
        SX126x(esp, cs=CS, reset=RESET, busy=BUSY, sf=4)
    with pytest.raises(ValueError):
        SX126x(esp, cs=CS, reset=RESET, busy=BUSY, bw=42)
    with pytest.raises(ValueError):
        SX126x(esp, cs=CS, reset=RESET, busy=BUSY, cr=9)
