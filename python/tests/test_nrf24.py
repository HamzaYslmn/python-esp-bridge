"""espbridge.drivers.nrf24.NRF24: nRF24L01+ radio — protocol-level (no hardware).

The module isn't wired to any board, so these tests drive a fake SPI/GPIO and
assert the SPI command bytes and CE toggling match the datasheet TX/RX flow.
"""
import pytest

from espbridge.drivers.nrf24 import NRF24

# command / register constants mirrored from the driver for readable asserts
W_REGISTER = 0x20
R_RX_PAYLOAD = 0x61
W_TX_PAYLOAD = 0xA0
CONFIG = 0x00
RF_CH = 0x05
PWR_UP = 1 << 1
EN_CRC = 1 << 3
TX_DS = 1 << 5
RX_DR = 1 << 6


class FakeSpi:
    """Records (tx, cs) transfers; returns canned rx of len == len(tx).

    `status_byte` is placed at rx[0] (the nRF24 clocks STATUS out first on
    every command); `payload` fills the rest (used for R_RX_PAYLOAD reads).
    """

    def __init__(self, status_byte=0x00, payload=b""):
        self.inited = None
        self.transfers = []          # list of (tx_bytes, cs)
        self.status_byte = status_byte
        self.payload = payload

    def init(self, *, sck=18, miso=19, mosi=23, freq=1_000_000,
             mode=0, msb_first=True, host=0):
        self.inited = dict(sck=sck, miso=miso, mosi=mosi, freq=freq,
                           mode=mode, msb_first=msb_first)

    def transfer(self, tx, *, cs=None, host=0):
        tx = bytes(tx)
        self.transfers.append((tx, cs))
        rx = bytearray(len(tx))
        if rx:
            rx[0] = self.status_byte
        # fill remaining bytes from the canned payload (for R_RX_PAYLOAD)
        for i, b in enumerate(self.payload):
            if 1 + i < len(rx):
                rx[1 + i] = b
        return bytes(rx)


class FakeGpio:
    def __init__(self):
        self.modes = []
        self.writes = []             # list of (pin, level)

    def mode(self, pin, mode):
        self.modes.append((pin, mode))

    def write(self, pin, value, *, verify=False):
        self.writes.append((pin, 1 if value else 0))
        return 1 if value else 0


class FakeEsp:
    def __init__(self, status_byte=0x00, payload=b""):
        self.spi = FakeSpi(status_byte, payload)
        self.gpio = FakeGpio()


def _writes_to(esp, reg):
    """All payloads written to register `reg` (data after the command byte)."""
    cmd = W_REGISTER | reg
    return [tx[1:] for tx, cs in esp.spi.transfers if tx and tx[0] == cmd]


def test_construct_configures_radio():
    esp = FakeEsp()
    NRF24(esp, cs=5, ce=17)
    # SPI brought up in mode 0, MSB-first.
    assert esp.spi.inited["mode"] == 0
    assert esp.spi.inited["msb_first"] is True
    # CONFIG written with PWR_UP and EN_CRC set.
    cfg = _writes_to(esp, CONFIG)
    assert cfg, "CONFIG was never written"
    assert cfg[0][0] & PWR_UP
    assert cfg[0][0] & EN_CRC
    # RF channel defaults to 76.
    assert _writes_to(esp, RF_CH)[0] == bytes([76])
    # CE and CS were set up as outputs.
    pins = {pin: mode for pin, mode in esp.gpio.modes}
    assert pins[17] == "output" and pins[5] == "output"


def test_send_writes_payload_and_toggles_ce():
    # STATUS with TX_DS set -> send() should see success.
    esp = FakeEsp(status_byte=TX_DS)
    nrf = NRF24(esp, cs=5, ce=17, payload_size=5)
    esp.spi.transfers.clear()
    esp.gpio.writes.clear()

    ok = nrf.send(b"hello")
    assert ok is True

    # W_TX_PAYLOAD frame carries exactly the (padded) data.
    tx_frames = [tx for tx, cs in esp.spi.transfers if tx and tx[0] == W_TX_PAYLOAD]
    assert tx_frames == [bytes([W_TX_PAYLOAD]) + b"hello"]
    # CE was pulsed: at least one high then a return to low.
    ce_levels = [lvl for pin, lvl in esp.gpio.writes if pin == 17]
    assert 1 in ce_levels and ce_levels[-1] == 0


def test_send_returns_false_on_max_rt():
    MAX_RT = 1 << 4
    esp = FakeEsp(status_byte=MAX_RT)
    nrf = NRF24(esp, cs=5, ce=17)
    assert nrf.send(b"x") is False


def test_read_issues_r_rx_payload_and_returns_payload():
    esp = FakeEsp(status_byte=RX_DR, payload=b"world")
    nrf = NRF24(esp, cs=5, ce=17, payload_size=5)
    esp.spi.transfers.clear()

    data = nrf.read()
    assert data == b"world"
    rx_frames = [tx for tx, cs in esp.spi.transfers if tx and tx[0] == R_RX_PAYLOAD]
    assert rx_frames, "R_RX_PAYLOAD was never issued"
    # read of payload_size bytes -> command byte + 5 placeholder bytes
    assert len(rx_frames[0]) == 1 + 5


def test_available_true_on_rx_dr():
    esp = FakeEsp(status_byte=RX_DR)
    nrf = NRF24(esp, cs=5, ce=17)
    assert nrf.available() is True


def test_bad_args_rejected():
    esp = FakeEsp()
    with pytest.raises(ValueError):
        NRF24(esp, cs=5, ce=17, channel=200)
    with pytest.raises(ValueError):
        NRF24(esp, cs=5, ce=17, data_rate="5mbps")
    with pytest.raises(ValueError):
        NRF24(esp, cs=5, ce=17, payload_size=64)
    with pytest.raises(ValueError):
        NRF24(esp, cs=5, ce=17, address=b"abc")


def test_receive_returns_one_packet():
    """receive() — the uniform radio API shared with the LoRa drivers: a packet
    waiting (RX_DR set) is read and returned, and RX is left (CE dropped)."""
    esp = FakeEsp(status_byte=RX_DR, payload=b"hello")
    nrf = NRF24(esp, cs=5, ce=17, payload_size=5)
    assert nrf.receive(timeout=0.5) == b"hello"
    ce_writes = [lvl for pin, lvl in esp.gpio.writes if pin == 17]
    assert 1 in ce_writes and ce_writes[-1] == 0   # entered RX, then stopped
