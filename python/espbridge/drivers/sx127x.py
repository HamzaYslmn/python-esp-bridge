"""Semtech SX1276/77/78/79 — LoRa transceiver (HopeRF RFM95/96/97/98).

A "bring your own" SPI driver over the bridge's ``spi`` primitive: long-range
LoRa radio for the 433 / 868 / 915 MHz ISM bands. No firmware change needed.

    from espbridge import Bridge

    with Bridge() as esp:
        # NSS/CS on GPIO5, RESET on GPIO14, default VSPI pins (sck=18/miso=19/mosi=23)
        lora = esp.sx127x(cs=5, reset=14, freq=868_000_000)

        # --- transmitter ---
        lora.send(b"hello world")

        # --- receiver ---
        pkt = lora.receive(timeout=5.0)   # bytes, or None on timeout / CRC error
        if pkt is not None:
            print("got", pkt)

``esp.rfm95(...)`` is an alias for the same class (the RFM9x modules are an
SX1276 plus a crystal and matching network).

Both ends must agree on frequency, spreading factor (``sf``), bandwidth
(``bw``), coding rate (``cr``) and ``sync_word`` or they will not hear each
other. The default 0x12 sync word is the convention for private LoRa networks
(0x34 is reserved for LoRaWAN).

NOTE: this driver was written and verified against the Semtech SX1276 datasheet
(rev. 7, register map in LoRa mode) and exercised at the protocol level with a
fake SPI bus — no SX1276 module is wired to the project's dev boards, so it has
not been bench-tested against real silicon. Register addresses, the Frf formula
and the TX/RX sequences below cite the datasheet sections they come from.
"""
from __future__ import annotations

import time

# --- Register map, LoRa mode (SX1276 datasheet rev.7, section 6.4, table 41/85) ---
_REG_FIFO = 0x00
_REG_OP_MODE = 0x01
_REG_FRF_MSB = 0x06          # Frf[23:16]
_REG_FRF_MID = 0x07          # Frf[15:8]
_REG_FRF_LSB = 0x08          # Frf[7:0]
_REG_PA_CONFIG = 0x09
_REG_FIFO_ADDR_PTR = 0x0D
_REG_FIFO_TX_BASE_ADDR = 0x0E
_REG_FIFO_RX_BASE_ADDR = 0x0F
_REG_FIFO_RX_CURRENT_ADDR = 0x10
_REG_IRQ_FLAGS = 0x12
_REG_RX_NB_BYTES = 0x13
_REG_MODEM_CONFIG_1 = 0x1D
_REG_MODEM_CONFIG_2 = 0x1E
_REG_PAYLOAD_LENGTH = 0x22
_REG_SYNC_WORD = 0x39
_REG_VERSION = 0x42          # reads 0x12 on SX1276 silicon
_REG_PA_DAC = 0x4D

# --- RegOpMode values (datasheet table 42). Bit 7 = LongRangeMode (LoRa). ---
_LORA = 0x80                 # LongRangeMode=1 (must be set while in SLEEP)
_MODE_SLEEP = 0x00
_MODE_STDBY = 0x01
_MODE_TX = 0x03
_MODE_RX_CONTINUOUS = 0x05

# --- RegIrqFlags bits (datasheet table 23) ---
_IRQ_RX_DONE = 0x40
_IRQ_PAYLOAD_CRC_ERROR = 0x20
_IRQ_TX_DONE = 0x08

# --- RegPaConfig / RegPaDac (datasheet 5.4.3) ---
_PA_BOOST = 0x80             # PaSelect bit 7: route to PA_BOOST pin (up to +17dBm)
_PA_DAC_DEFAULT = 0x84       # normal mode
_PA_DAC_HIGH_POWER = 0x87    # enables +20dBm on PA_BOOST

_FXOSC = 32_000_000          # crystal frequency (Hz)


class SX127x:
    """SX1276/77/78/79 LoRa transceiver over the bridge SPI bus."""

    def __init__(self, bridge, *, cs, reset=None, dio0=None,
                 freq=433_000_000, tx_power=17, sf=7, bw=125_000, cr=5,
                 sync_word=0x12, sck=18, miso=19, mosi=23, spi_freq=1_000_000):
        if not 0x00 <= int(sync_word) <= 0xFF:
            raise ValueError(f"sync_word must be a byte 0x00..0xFF, got {sync_word:#x}")
        self._spi = bridge.spi
        self._gpio = bridge.gpio
        self._cs = cs
        self._reset = reset
        self._dio0 = dio0

        # Bring up the SPI host only when the user gave bus pins; otherwise assume
        # esp.spi.init(...) was already called. SX1276 is SPI mode 0, MSB-first.
        if sck is not None and miso is not None and mosi is not None:
            self._spi.init(sck=sck, miso=miso, mosi=mosi, freq=spi_freq, mode=0)

        # Hardware reset: NRESET is active-low; hold low >=100us, then wait 5ms (datasheet 7.2.2).
        if reset is not None:
            self._gpio.mode(reset, "output")
            self._gpio.write(reset, 0)
            time.sleep(0.010)
            self._gpio.write(reset, 1)
            time.sleep(0.010)

        if dio0 is not None:
            self._gpio.mode(dio0, "input")

        version = self._read(_REG_VERSION)
        if version != 0x12:
            raise RuntimeError(
                f"SX1276 not found on CS pin {cs}: RegVersion (0x42) read "
                f"{version:#04x}, expected 0x12 — check wiring/power/CS"
            )

        # LoRa mode can only be selected from SLEEP (datasheet 4.1.6.4): write the
        # LongRangeMode bit together with SLEEP, then drop to STANDBY to configure.
        self._write(_REG_OP_MODE, _LORA | _MODE_SLEEP)
        self._write(_REG_OP_MODE, _LORA | _MODE_STDBY)

        self.set_frequency(freq)

        # FIFO is 256 bytes shared between TX and RX; give each the whole span.
        self._write(_REG_FIFO_TX_BASE_ADDR, 0x00)
        self._write(_REG_FIFO_RX_BASE_ADDR, 0x00)

        self._set_modem_config(sf, bw, cr)
        self._write(_REG_SYNC_WORD, int(sync_word) & 0xFF)
        self.set_tx_power(tx_power)

        self.standby()

    # ----- low-level register access (datasheet 4.3 SPI interface) -----
    # Single-byte access: byte0 = address, wnr bit 7 (1=write, 0=read), then data.
    # CS is held low for the whole 2-byte frame by spi.transfer(..., cs=).

    def _read(self, reg: int) -> int:
        """Read one register (transfer [reg & 0x7F, 0x00]; data clocked in on byte 1)."""
        rx = self._spi.transfer(bytes([reg & 0x7F, 0x00]), cs=self._cs)
        return rx[1]

    def _write(self, reg: int, value: int) -> None:
        """Write one register (transfer [reg | 0x80, value])."""
        self._spi.transfer(bytes([reg | 0x80, value & 0xFF]), cs=self._cs)

    # ----- configuration -----
    def _set_modem_config(self, sf: int, bw: int, cr: int) -> None:
        """Program SF, bandwidth, coding rate and enable CRC."""
        if not 6 <= sf <= 12:
            raise ValueError(f"sf must be 6..12, got {sf}")
        if not 5 <= cr <= 8:
            raise ValueError(f"cr must be 5..8 (i.e. 4/5..4/8), got {cr}")

        # RegModemConfig1 (0x1D): Bw[7:4] | CodingRate[3:1] | ImplicitHeaderModeOn[0].
        # Bw codes (datasheet table 87): 7=125k, 8=250k, 9=500k. CodingRate = cr-4.
        bw_codes = {7_800: 0, 10_400: 1, 15_600: 2, 20_800: 3, 31_250: 4,
                    41_700: 5, 62_500: 6, 125_000: 7, 250_000: 8, 500_000: 9}
        if bw not in bw_codes:
            raise ValueError(f"bw must be one of {sorted(bw_codes)} Hz, got {bw}")
        self._write(_REG_MODEM_CONFIG_1, (bw_codes[bw] << 4) | ((cr - 4) << 1))

        # RegModemConfig2 (0x1E): SpreadingFactor[7:4] | TxContinuousMode[3] |
        # RxPayloadCrcOn[2] | SymbTimeout[9:8]. Set SF and CRC-on (bit 2).
        self._write(_REG_MODEM_CONFIG_2, (sf << 4) | 0x04)

    def set_frequency(self, hz: int) -> None:
        """Set the carrier frequency in Hz (e.g. 433/868/915 MHz)."""
        if not 137_000_000 <= hz <= 1_020_000_000:
            raise ValueError(f"frequency {hz} Hz out of SX1276 range (137 MHz..1020 MHz)")
        # Frf = freq * 2^19 / FXOSC  (datasheet 4.1.4); split into three 8-bit regs.
        frf = round(hz * (1 << 19) / _FXOSC)
        self._write(_REG_FRF_MSB, (frf >> 16) & 0xFF)
        self._write(_REG_FRF_MID, (frf >> 8) & 0xFF)
        self._write(_REG_FRF_LSB, frf & 0xFF)

    def set_tx_power(self, dbm: int) -> None:
        """Set TX power in dBm on the PA_BOOST pin (2..20 dBm)."""
        if not 2 <= dbm <= 20:
            raise ValueError(f"tx_power must be 2..20 dBm (PA_BOOST), got {dbm}")
        if dbm > 17:
            # +20dBm needs the high-power DAC; OutputPower then maps 5..20 -> field 0..15.
            self._write(_REG_PA_DAC, _PA_DAC_HIGH_POWER)
            self._write(_REG_PA_CONFIG, _PA_BOOST | (dbm - 5))
        else:
            self._write(_REG_PA_DAC, _PA_DAC_DEFAULT)
            # On PA_BOOST, Pout = 17 - (15 - OutputPower) = OutputPower + 2 dBm.
            self._write(_REG_PA_CONFIG, _PA_BOOST | (dbm - 2))

    # ----- power modes -----
    def standby(self) -> None:
        """Put the radio in STANDBY (oscillator on, ready to TX/RX)."""
        self._write(_REG_OP_MODE, _LORA | _MODE_STDBY)

    def sleep(self) -> None:
        """Put the radio in low-power SLEEP."""
        self._write(_REG_OP_MODE, _LORA | _MODE_SLEEP)

    # ----- data path -----
    def send(self, data: bytes) -> None:
        """Transmit a packet (1..255 bytes), blocking until TxDone."""
        data = bytes(data)
        if not 1 <= len(data) <= 255:
            raise ValueError(f"payload must be 1..255 bytes, got {len(data)}")

        # TX sequence (datasheet 4.1.7): STANDBY, point FIFO at TX base, fill FIFO,
        # set PayloadLength, switch to TX, wait for TxDone, then clear the flag.
        self.standby()
        self._write(_REG_FIFO_ADDR_PTR, 0x00)          # TX base addr (set to 0 above)
        for b in data:
            self._write(_REG_FIFO, b)
        self._write(_REG_PAYLOAD_LENGTH, len(data))
        self._write(_REG_OP_MODE, _LORA | _MODE_TX)

        while not (self._read(_REG_IRQ_FLAGS) & _IRQ_TX_DONE):
            time.sleep(0.001)
        self._write(_REG_IRQ_FLAGS, _IRQ_TX_DONE)      # write-1-to-clear

    def receive(self, timeout: float | None = None) -> bytes | None:
        """Receive one packet. Returns the payload bytes, or None on timeout
        or CRC error. ``timeout`` is in seconds; None waits forever."""
        # RX sequence (datasheet 4.1.6): enter RX-continuous and watch RxDone.
        self._write(_REG_OP_MODE, _LORA | _MODE_RX_CONTINUOUS)

        deadline = None if timeout is None else time.monotonic() + timeout
        while not (self._read(_REG_IRQ_FLAGS) & _IRQ_RX_DONE):
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.001)

        flags = self._read(_REG_IRQ_FLAGS)
        # Clear RxDone (and any CRC-error flag) by writing 1s back.
        self._write(_REG_IRQ_FLAGS, _IRQ_RX_DONE | _IRQ_PAYLOAD_CRC_ERROR)
        if flags & _IRQ_PAYLOAD_CRC_ERROR:
            return None

        # Read the received length and point the FIFO at where this packet landed.
        n = self._read(_REG_RX_NB_BYTES)
        self._write(_REG_FIFO_ADDR_PTR, self._read(_REG_FIFO_RX_CURRENT_ADDR))
        return bytes(self._read(_REG_FIFO) for _ in range(n))
