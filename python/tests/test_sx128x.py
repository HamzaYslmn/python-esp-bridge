"""espbridge.drivers.sx128x.SX128x: 2.4 GHz LoRa transceiver, command protocol.

Hardware-free: a fake SPI records every framed transfer and a fake GPIO reports
BUSY ready (low) while logging pin writes. The fake SPI returns canned replies
for read commands (GetIrqStatus / GetRxBufferStatus / ReadBuffer) keyed by opcode.
"""
import pytest

from espbridge.drivers.sx128x import SX128x

# Opcodes mirrored from the driver for assertions.
SET_STANDBY = 0x80
SET_PACKET_TYPE = 0x8A
SET_RF_FREQUENCY = 0x86
SET_BUFFER_BASE = 0x8F
SET_MODULATION = 0x8B
SET_PACKET_PARAMS = 0x8C
SET_TX_PARAMS = 0x8E
WRITE_BUFFER = 0x1A
READ_BUFFER = 0x1B
SET_TX = 0x83
SET_RX = 0x82
GET_IRQ_STATUS = 0x15
CLR_IRQ_STATUS = 0x97
GET_RX_BUFFER_STATUS = 0x17
SET_DIO_IRQ = 0x8D

IRQ_TX_DONE = 0x0001
IRQ_RX_DONE = 0x0002


class FakeSpi:
    def __init__(self):
        self.inited = None
        self.frames = []          # list of (tx_bytes, cs)
        # opcode -> reply bytes appended after the status byte
        self.canned = {}

    def init(self, *, sck=18, miso=19, mosi=23, freq=1_000_000, mode=0, msb_first=True):
        self.inited = (sck, miso, mosi, freq, mode, msb_first)

    def transfer(self, tx, *, cs=None):
        tx = bytes(tx)
        self.frames.append((tx, cs))
        rx = bytearray(len(tx))
        reply = self.canned.get(tx[0])
        if reply is not None:
            # Driver reads bytes after the (args + status) prefix; place the
            # reply at the tail of the rx buffer so rx[len(args)+2:] picks it up.
            rx[len(tx) - len(reply):] = reply
        return bytes(rx)


class FakeGpio:
    def __init__(self, busy_pin, dio1_pin=None):
        self.modes = {}
        self.writes = []          # list of (pin, level)
        self.reads = []           # list of pins read
        self._busy_pin = busy_pin
        self._dio1_pin = dio1_pin

    def mode(self, pin, mode):
        self.modes[pin] = mode

    def write(self, pin, level, *, verify=False):
        self.writes.append((pin, 1 if level else 0))
        return 1 if level else 0

    def read(self, pin):
        self.reads.append(pin)
        if pin == self._busy_pin:
            return 0              # BUSY low == ready
        if pin == self._dio1_pin:
            return 1              # DIO1 asserted == IRQ pending
        return 0


class FakeEsp:
    def __init__(self, busy=2, dio1=15):
        self.spi = FakeSpi()
        self.gpio = FakeGpio(busy, dio1)


def make(**kw):
    esp = FakeEsp()
    radio = SX128x(esp, cs=5, reset=4, busy=2, dio1=15, **kw)
    return esp, radio


def frames_with(esp, opcode):
    return [tx for tx, _cs in esp.spi.frames if tx[0] == opcode]


def test_spi_initialised_mode0_msb():
    esp, _ = make()
    assert esp.spi.inited == (18, 19, 23, 8_000_000, 0, True)


def test_reset_pulse_high_low_high():
    esp, _ = make()
    reset_writes = [lvl for pin, lvl in esp.gpio.writes if pin == 4]
    assert reset_writes[:3] == [1, 0, 1]   # release, assert(active low), release
    assert esp.gpio.modes[4] == "output"
    assert esp.gpio.modes[2] == "input"    # BUSY is an input


def test_set_packet_type_lora():
    esp, _ = make()
    pt = frames_with(esp, SET_PACKET_TYPE)
    assert pt and pt[0] == bytes([SET_PACKET_TYPE, 0x01])   # 0x01 == LoRa


def test_set_rf_frequency_2_4ghz_bytes():
    esp, _ = make(freq=2_400_000_000)
    # freq_reg = round(2.4e9 * 2**18 / 52e6) = 12098954 = 0xB89D8A
    assert round(2_400_000_000 * 2**18 / 52_000_000) == 0xB89D8A
    f = frames_with(esp, SET_RF_FREQUENCY)
    assert f and f[0] == bytes([SET_RF_FREQUENCY, 0xB8, 0x9D, 0x8A])


def test_modulation_params_sf_high_nibble():
    esp, _ = make(sf=7, bw=400_000, cr=1)
    m = frames_with(esp, SET_MODULATION)
    # SF7 -> 0x70 (high nibble), BW 400k -> 0x26, CR 1 -> 0x01
    assert m and m[0] == bytes([SET_MODULATION, 0x70, 0x26, 0x01])


def test_dio_irq_params_routes_txdone_rxdone():
    esp, _ = make()
    d = frames_with(esp, SET_DIO_IRQ)
    assert d and d[0][1:3] == bytes([0x40, 0x03])   # IRQ mask = TxDone|RxDone|Timeout


def test_busy_polled_before_commands():
    esp, _ = make()
    # BUSY (pin 2) is read at least once during init's command sequence.
    assert 2 in esp.gpio.reads


def test_send_writes_buffer_then_tx_and_completes_on_txdone():
    esp, radio = make()
    esp.spi.canned[GET_IRQ_STATUS] = bytes([0x00, IRQ_TX_DONE])  # TxDone set
    esp.spi.frames.clear()

    radio.send(b"\xde\xad\xbe\xef")

    wb = frames_with(esp, WRITE_BUFFER)
    assert wb and wb[0] == bytes([WRITE_BUFFER, 0x00, 0xDE, 0xAD, 0xBE, 0xEF])
    # payload length propagated into SetPacketParams (byte index 4 = payload len)
    pp = frames_with(esp, SET_PACKET_PARAMS)
    assert pp and pp[-1][4] == 4
    tx = frames_with(esp, SET_TX)
    assert tx, "SetTx must be issued"
    # IRQ polled and cleared
    assert frames_with(esp, GET_IRQ_STATUS)
    assert frames_with(esp, CLR_IRQ_STATUS)


def test_receive_reads_buffer_status_then_payload():
    esp, radio = make()
    payload = b"hi 2.4G"
    esp.spi.canned[GET_IRQ_STATUS] = bytes([0x00, IRQ_RX_DONE])           # RxDone
    esp.spi.canned[GET_RX_BUFFER_STATUS] = bytes([len(payload), 0x00])    # len, start
    esp.spi.canned[READ_BUFFER] = payload
    esp.spi.frames.clear()

    got = radio.receive(timeout=1.0)
    assert got == payload
    assert frames_with(esp, SET_RX)
    assert frames_with(esp, GET_RX_BUFFER_STATUS)
    rb = frames_with(esp, READ_BUFFER)
    assert rb and rb[0][1] == 0x00   # ReadBuffer offset == rxStartBufferPointer


def test_receive_returns_none_on_timeout():
    esp = FakeEsp()
    # No DIO1 wiring path that asserts + no canned IRQ -> wait_irq times out.
    esp.gpio._dio1_pin = None        # DIO1 never asserts -> IRQ never read
    radio = SX128x(esp, cs=5, reset=4, busy=2, dio1=15)
    assert radio.receive(timeout=0.05) is None


def test_invalid_args_rejected():
    esp = FakeEsp()
    with pytest.raises(ValueError):
        SX128x(esp, cs=5, reset=4, busy=2, freq=900_000_000)   # sub-GHz
    with pytest.raises(ValueError):
        SX128x(esp, cs=5, reset=4, busy=2, tx_power=20)        # over +13 dBm
    with pytest.raises(ValueError):
        SX128x(esp, cs=5, reset=4, busy=2, sf=4)               # SF below 5
    with pytest.raises(ValueError):
        SX128x(esp, cs=5, reset=4, busy=2, bw=123)             # bad bandwidth
    with pytest.raises(ValueError):
        SX128x(esp, cs=5, reset=4, busy=2, cr=9)               # CR out of range
