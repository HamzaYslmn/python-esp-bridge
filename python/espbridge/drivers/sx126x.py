"""Semtech SX1262 / SX1268 (SX126x family) LoRa transceiver over the bridge's SPI.

The SX126x is the LoRa radio on Heltec WiFi LoRa 32 V3, TTGO/LilyGO T-Beam (newer
revisions), and Ebyte E22 modules (433 / 868 / 915 MHz). Unlike the older SX127x,
it is a *command-based* part: you never touch raw registers for normal operation —
you send opcode + argument frames over SPI, and a dedicated BUSY pin tells you when
the internal MCU has finished the previous command.

    from espbridge import Bridge
    from espbridge.drivers.sx126x import SX126x

    with Bridge() as esp:
        radio = esp.sx126x(cs=8, reset=12, busy=13, dio1=14, freq=868_000_000)

        # transmitter
        radio.send(b"hello lora")

        # receiver
        msg = radio.receive(timeout=5.0)   # bytes, or None on timeout / CRC error
        if msg is not None:
            print(msg)

BUSY handshake: before *every* SPI command the host must wait for BUSY to go low,
otherwise the command is silently dropped. This driver polls ``bridge.gpio.read``
in a bounded loop (see ``_wait_busy``) ahead of each transfer.

Wiring (typical Heltec V3): CS=8, RESET=12, BUSY=13, DIO1=14, SCK=9, MISO=11,
MOSI=10. Pass your board's pins to the constructor.

STATUS: this driver is written from the Semtech SX1261/2 datasheet (Rev. 1.2,
DS.SX1261-2.W.APP, June 2019) and Semtech's reference C library. It is NOT wired
to the project's dev boards; it has been validated at the protocol level only
(byte-exact command framing, BUSY polling, TX/RX flow) via the hardware-free test
in tests/test_sx126x.py — not against a physical radio.
"""
from __future__ import annotations

import time

# --- Command opcodes (SX1261/2 datasheet section 13; cited inline) -----------
_SET_STANDBY = 0x80          # 13.1.2  SetStandby(StandbyConfig)
_SET_PACKET_TYPE = 0x8A      # 13.4.2  SetPacketType(PacketType)
_SET_RF_FREQUENCY = 0x86     # 13.4.1  SetRfFrequency(Freq[31:0])
_SET_PA_CONFIG = 0x95        # 13.1.14 SetPaConfig(dutyCycle, hpMax, devSel, paLut)
_SET_TX_PARAMS = 0x8E        # 13.4.4  SetTxParams(power, rampTime)
_SET_BUFFER_BASE = 0x8F      # 13.4.3  SetBufferBaseAddress(txBase, rxBase)
_SET_MODULATION = 0x8B       # 13.4.5  SetModulationParams(SF, BW, CR, LdrOpt)
_SET_PACKET_PARAMS = 0x8C    # 13.4.6  SetPacketParams(...)
_WRITE_BUFFER = 0x0E         # 13.2.3  WriteBuffer(offset, data...)
_READ_BUFFER = 0x1E          # 13.2.4  ReadBuffer(offset, NOP, -> data...)
_SET_TX = 0x83               # 13.1.5  SetTx(timeout[23:0])
_SET_RX = 0x82               # 13.1.6  SetRx(timeout[23:0])
_GET_IRQ_STATUS = 0x12       # 13.3.4  GetIrqStatus() -> Irq[15:0]
_CLEAR_IRQ_STATUS = 0x02     # 13.3.5  ClearIrqStatus(Irq[15:0])
_SET_DIO_IRQ_PARAMS = 0x08   # 13.3.1  SetDioIrqParams(irq, dio1, dio2, dio3)
_GET_RX_BUFFER_STATUS = 0x13 # 13.5.2  GetRxBufferStatus() -> payloadLen, startPtr
_GET_STATUS = 0xC0           # 13.5.1  GetStatus() -> status
_SET_SLEEP = 0x84            # 13.1.1  SetSleep(sleepConfig)

# --- Argument constants ------------------------------------------------------
_STDBY_RC = 0x00             # SetStandby: 13 MHz RC oscillator
_PKT_TYPE_LORA = 0x01        # SetPacketType: LoRa (0x00 = GFSK)
_RAMP_200U = 0x04            # SetTxParams rampTime = 200 us

# SetModulationParams codes (datasheet table 13-47 / 13-48 / 13-49)
_BW = {                      # bandwidth (Hz) -> BW code
    7_800: 0x00, 10_400: 0x08, 15_600: 0x01, 20_800: 0x09, 31_250: 0x02,
    41_700: 0x0A, 62_500: 0x03, 125_000: 0x04, 250_000: 0x05, 500_000: 0x06,
}
# Coding rate: register code == cr - 4  (cr 5..8 -> 0x01..0x04, "4/5".."4/8")

# IRQ bit masks (datasheet table 13-29)
_IRQ_TX_DONE = 0x0001
_IRQ_RX_DONE = 0x0002
_IRQ_CRC_ERR = 0x0040
_IRQ_TIMEOUT = 0x0200
_IRQ_ALL = 0xFFFF

# SetTx/SetRx timeout is in steps of 15.625 us; 0 = single mode / no timeout.
_BUSY_TIMEOUT = 0.1          # s, max wait for BUSY to fall (commands finish in <100 us)


class SX126x:
    """Driver for the Semtech SX126x command-based LoRa transceiver."""

    def __init__(self, bridge, *, cs: int, reset: int, busy: int,
                 dio1: int | None = None, freq: int = 868_000_000,
                 tx_power: int = 14, sf: int = 7, bw: int = 125_000, cr: int = 5,
                 sck: int | None = 18, miso: int | None = 19, mosi: int | None = 23,
                 spi_freq: int = 2_000_000):
        if not 5 <= sf <= 12:
            raise ValueError(f"sf must be 5..12, got {sf}")
        if bw not in _BW:
            raise ValueError(f"bw must be one of {sorted(_BW)} Hz, got {bw}")
        if not 5 <= cr <= 8:
            raise ValueError(f"cr must be 5..8 (4/5..4/8), got {cr}")
        if not -9 <= tx_power <= 22:
            raise ValueError(f"tx_power must be -9..22 dBm, got {tx_power}")

        self._b = bridge
        self._spi = bridge.spi
        self._gpio = bridge.gpio
        self._cs = cs
        self._reset = reset
        self._busy = busy
        self._dio1 = dio1
        self._freq = int(freq)
        self._sf = sf
        self._bw_code = _BW[bw]
        self._cr_code = cr - 4
        self._preamble = 8

        if sck is not None and miso is not None and mosi is not None:
            self._spi.init(sck=sck, miso=miso, mosi=mosi, freq=spi_freq, mode=0)

        # Control pins: RESET + CS are outputs we drive; BUSY (+ DIO1) are inputs.
        self._gpio.mode(reset, "output")
        self._gpio.mode(cs, "output")
        self._gpio.write(cs, 1)            # CS idle high
        self._gpio.mode(busy, "input")
        if dio1 is not None:
            self._gpio.mode(dio1, "input")

        self._reset_chip()
        self._command(_SET_STANDBY, _STDBY_RC)
        self._command(_SET_PACKET_TYPE, _PKT_TYPE_LORA)
        self.set_frequency(self._freq)
        # PA config for SX1262 high-power PA, +22 dBm capable (table 13-21).
        self._command(_SET_PA_CONFIG, 0x04, 0x07, 0x00, 0x01)
        self._command(_SET_TX_PARAMS, tx_power & 0xFF, _RAMP_200U)
        self._command(_SET_BUFFER_BASE, 0x00, 0x00)
        self._set_modulation()
        self._set_packet_params(0)        # payload len set per-frame in send()
        # Route TxDone | RxDone | Timeout to the global IRQ and onto DIO1.
        mask = _IRQ_TX_DONE | _IRQ_RX_DONE | _IRQ_TIMEOUT
        self._command(_SET_DIO_IRQ_PARAMS,
                      (mask >> 8) & 0xFF, mask & 0xFF,   # IRQ mask
                      (mask >> 8) & 0xFF, mask & 0xFF,   # DIO1 mask
                      0x00, 0x00, 0x00, 0x00)            # DIO2, DIO3 unused

    # --- low-level command transport ----------------------------------------
    def _wait_busy(self) -> None:
        """Block until BUSY is low (chip ready); raise on timeout.

        The SX126x asserts BUSY while its internal MCU processes a command.
        Sending SPI while BUSY is high is undefined behaviour, so every command
        must be preceded by this handshake (datasheet section 8.3.1)."""
        deadline = time.monotonic() + _BUSY_TIMEOUT
        while self._gpio.read(self._busy):
            if time.monotonic() > deadline:
                raise TimeoutError("SX126x BUSY stuck high — wiring/power problem?")

    def _command(self, opcode: int, *args: int) -> None:
        """Wait for BUSY, then send opcode + args as one CS-low frame."""
        self._wait_busy()
        self._spi.transfer(bytes([opcode, *args]), cs=self._cs)

    def _read_command(self, opcode: int, nresp: int) -> bytes:
        """Send a status/get command and return its `nresp` response bytes.

        The SX126x clocks the response out *during* the same frame: the first
        byte returned for opcode + NOP is the chip status, the data follows. We
        send opcode followed by `nresp` NOP (0x00) bytes and drop the echoed
        opcode byte, returning the `nresp` response bytes (datasheet 13.5)."""
        self._wait_busy()
        rx = self._spi.transfer(bytes([opcode]) + b"\x00" * nresp, cs=self._cs)
        return rx[1:]

    # --- configuration -------------------------------------------------------
    def _reset_chip(self) -> None:
        """Hardware reset: pull RESET low ~1 ms, release, wait for BUSY low."""
        self._gpio.write(self._reset, 0)
        time.sleep(0.002)
        self._gpio.write(self._reset, 1)
        time.sleep(0.005)
        self._wait_busy()

    def _set_modulation(self) -> None:
        # SetModulationParams(SF, BW, CR, LowDataRateOptimize). LDRO must be on
        # when symbol time > 16.38 ms (high SF + low BW); enable it for SF11/12.
        ldro = 0x01 if self._sf >= 11 else 0x00
        self._command(_SET_MODULATION, self._sf, self._bw_code, self._cr_code, ldro)

    def _set_packet_params(self, payload_len: int) -> None:
        # SetPacketParams: preamble[15:8], preamble[7:0], headerType,
        # payloadLen, crcOn, invertIQ. 0x00 header = variable-length (explicit),
        # 0x01 crc on, 0x00 standard IQ.
        self._command(_SET_PACKET_PARAMS,
                      (self._preamble >> 8) & 0xFF, self._preamble & 0xFF,
                      0x00, payload_len & 0xFF, 0x01, 0x00)

    def set_frequency(self, hz: int) -> None:
        """Set the RF carrier frequency in Hz (e.g. 868_000_000).

        Register value = round(freq * 2**25 / 32 MHz), 4 bytes big-endian
        (datasheet 13.4.1; FXTAL = 32 MHz)."""
        self._freq = int(hz)
        reg = round(self._freq * (1 << 25) / 32_000_000)
        self._command(_SET_RF_FREQUENCY,
                      (reg >> 24) & 0xFF, (reg >> 16) & 0xFF,
                      (reg >> 8) & 0xFF, reg & 0xFF)

    # --- IRQ helpers ---------------------------------------------------------
    def _irq_status(self) -> int:
        # GetIrqStatus returns: status byte, Irq[15:8], Irq[7:0].
        r = self._read_command(_GET_IRQ_STATUS, 3)
        return (r[1] << 8) | r[2]

    def _clear_irq(self, mask: int = _IRQ_ALL) -> None:
        self._command(_CLEAR_IRQ_STATUS, (mask >> 8) & 0xFF, mask & 0xFF)

    def _wait_irq(self, want: int, timeout: float | None) -> int:
        """Poll (or watch DIO1 then read) until any `want` bit is set.
        Returns the IRQ word; raises TimeoutError if `timeout` elapses."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            irq = self._irq_status()
            if irq & want:
                return irq
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("SX126x: IRQ wait timed out")
            time.sleep(0.001)

    # --- public API ----------------------------------------------------------
    def send(self, data: bytes) -> None:
        """Transmit a LoRa packet (1..255 bytes), blocking until TxDone."""
        data = bytes(data)
        if not 1 <= len(data) <= 255:
            raise ValueError(f"payload must be 1..255 bytes, got {len(data)}")
        self._command(_SET_STANDBY, _STDBY_RC)
        self._command(_SET_BUFFER_BASE, 0x00, 0x00)
        self._command(_WRITE_BUFFER, 0x00, *data)      # offset 0, then payload
        self._set_packet_params(len(data))
        self._clear_irq()
        self._command(_SET_TX, 0x00, 0x00, 0x00)       # timeout 0 = single TX
        self._wait_irq(_IRQ_TX_DONE, timeout=10.0)
        self._clear_irq()

    def receive(self, timeout: float | None = None) -> bytes | None:
        """Receive one LoRa packet; return its bytes, or None on timeout/CRC error.

        `timeout` is in seconds (None = block forever). The radio is put into
        single-RX with a continuous (no hardware) timeout; the wait is enforced
        host-side so any value is supported."""
        self._command(_SET_STANDBY, _STDBY_RC)
        self._set_packet_params(255)                   # max len before header decode
        self._clear_irq()
        self._command(_SET_RX, 0xFF, 0xFF, 0xFF)       # 0xFFFFFF = continuous RX
        try:
            irq = self._wait_irq(_IRQ_RX_DONE | _IRQ_TIMEOUT, timeout)
        except TimeoutError:
            self.standby()
            return None
        if irq & _IRQ_TIMEOUT or irq & _IRQ_CRC_ERR:
            self._clear_irq()
            return None
        # GetRxBufferStatus -> status, payloadLength, rxStartBufferPointer.
        st = self._read_command(_GET_RX_BUFFER_STATUS, 3)
        length, offset = st[1], st[2]
        # ReadBuffer(offset): opcode, offset, NOP, then `length` data bytes.
        rx = self._spi_read_buffer(offset, length)
        self._clear_irq()
        return rx

    def _spi_read_buffer(self, offset: int, length: int) -> bytes:
        self._wait_busy()
        tx = bytes([_READ_BUFFER, offset & 0xFF, 0x00]) + b"\x00" * length
        rx = self._spi.transfer(tx, cs=self._cs)
        return rx[3:3 + length]   # drop status echo (opcode, offset, NOP slots)

    def standby(self) -> None:
        """Put the radio into standby (STDBY_RC), stopping any TX/RX."""
        self._command(_SET_STANDBY, _STDBY_RC)

    def sleep(self) -> None:
        """Put the radio into sleep mode (warm start, retain config)."""
        self._command(_SET_SLEEP, 0x04)   # bit2 = warm start (retain configuration)

    def status(self) -> int:
        """Return the chip status byte (GetStatus): bits 6:4 chip mode,
        bits 3:1 command status."""
        return self._read_command(_GET_STATUS, 1)[0]
