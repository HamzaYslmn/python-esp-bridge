"""nRF24L01+ — 2.4GHz GFSK radio transceiver (SPI).

A "bring your own" driver over the bridge's SPI + GPIO primitives, no firmware
change. The nRF24L01+ is a packet radio with a hardware Enhanced ShockBurst
engine (auto-ACK + auto-retransmit) on its own 2.4GHz band — it is NOT WiFi or
BLE. Two modules talk to each other; a single module cannot loop back.

    from espbridge import Bridge

    # --- transmitter ---
    with Bridge() as esp:
        nrf = esp.nrf24(cs=5, ce=17, channel=76, address=b"1Node")
        ok = nrf.send(b"hello")          # blocks until ACK or retransmit-exhausted
        print("delivered" if ok else "no ACK")

    # --- receiver (a second board / module) ---
    with Bridge() as esp:
        nrf = esp.nrf24(cs=5, ce=17, channel=76, address=b"1Node")
        nrf.start_listening()
        while True:
            if nrf.available():
                print(nrf.read())        # bytes, payload_size long

NOTE ON TESTING: these modules are NOT wired to the project's dev boards, so
this driver is datasheet-based and validated at the protocol/byte level only
(see python/tests/test_nrf24.py) — it has not been exercised against real
hardware. The register layout, SPI command set and TX/RX handshake follow the
Nordic "nRF24L01+ Single Chip 2.4GHz Transceiver Product Specification v1.0".

Wiring: CS -> a GPIO (the bridge drives it low for each SPI frame via cs=);
CE -> a GPIO we toggle to arm TX / enable RX; SCK/MISO/MOSI -> the SPI host.
The module is 3.3V only and draws current spikes — decouple VCC well.
"""
from __future__ import annotations

import time

# --- SPI command words (PS v1.0, Table 19 "Command set") -------------------
# R_REGISTER / W_REGISTER are OR'd with the 5-bit register address.
_R_REGISTER = 0x00      # 000A AAAA — read register A
_W_REGISTER = 0x20      # 001A AAAA — write register A (power-down / standby only)
_R_RX_PAYLOAD = 0x61    # read 1..32 bytes from the top RX FIFO entry
_W_TX_PAYLOAD = 0xA0    # write 1..32 bytes to the TX FIFO
_FLUSH_TX = 0xE1        # flush TX FIFO
_FLUSH_RX = 0xE2        # flush RX FIFO
_NOP = 0xFF             # no-op; handy to clock out the STATUS byte

# --- register addresses (PS v1.0, Table 28 "Register map") -----------------
_CONFIG = 0x00
_EN_AA = 0x01           # Enhanced ShockBurst auto-ack per pipe
_EN_RXADDR = 0x02       # enabled RX pipes
_SETUP_AW = 0x03        # address width
_SETUP_RETR = 0x04      # auto-retransmit delay / count
_RF_CH = 0x05           # RF channel (0..125)
_RF_SETUP = 0x06        # data rate + output power
_STATUS = 0x07          # interrupt + FIFO status (returned on every SPI cmd)
_RX_ADDR_P0 = 0x0A      # RX pipe 0 address (also the auto-ACK pipe for a TX)
_TX_ADDR = 0x10         # transmit address
_RX_PW_P0 = 0x11        # RX pipe 0 payload width (static payloads)
_FIFO_STATUS = 0x17

# --- CONFIG register bits (PS v1.0, Table 28) ------------------------------
_EN_CRC = 1 << 3
_CRCO = 1 << 2          # CRC scheme: 0 = 1 byte, 1 (set) = 2 bytes
_PWR_UP = 1 << 1
_PRIM_RX = 1 << 0       # 1 = PRX (receiver), 0 = PTX (transmitter)

# --- STATUS register bits --------------------------------------------------
_RX_DR = 1 << 6         # data ready in RX FIFO  (write 1 to clear)
_TX_DS = 1 << 5         # data sent / ACK received (write 1 to clear)
_MAX_RT = 1 << 4        # max retransmits reached  (write 1 to clear; blocks TX)

# --- FIFO_STATUS bits ------------------------------------------------------
_RX_EMPTY = 1 << 0

# --- RF_SETUP data-rate encoding (PS v1.0, RF_DR_LOW=bit5, RF_DR_HIGH=bit4) -
#   1Mbps : low=0 high=0 ; 2Mbps : low=0 high=1 ; 250kbps : low=1 high=0
_RF_DR_LOW = 1 << 5
_RF_DR_HIGH = 1 << 4
_RF_PWR_0DBM = 0b11 << 1     # RF_PWR bits 2:1 = 11 -> 0 dBm (max output)
_DATA_RATES = {
    "250kbps": _RF_DR_LOW,
    "1mbps": 0,
    "2mbps": _RF_DR_HIGH,
}

_ADDR_LEN = 5           # we use the default 5-byte address width (SETUP_AW=0b11)


class NRF24:
    """nRF24L01+ 2.4GHz transceiver.

        nrf = esp.nrf24(cs=5, ce=17, channel=76, address=b"1Node")
        nrf.send(b"hi")              # TX, returns True on ACK
        nrf.start_listening()        # become a receiver
        if nrf.available():
            data = nrf.read()
    """

    def __init__(self, bridge, *, cs: int, ce: int, channel: int = 76,
                 data_rate: str = "1mbps", payload_size: int = 32,
                 address: bytes = b"\xe7\xe7\xe7\xe7\xe7",
                 sck: int | None = 18, miso: int | None = 19,
                 mosi: int | None = 23, freq: int = 8_000_000):
        if not 0 <= channel <= 125:
            raise ValueError(f"channel must be 0..125, got {channel}")
        if data_rate not in _DATA_RATES:
            raise ValueError(f"data_rate must be one of {sorted(_DATA_RATES)}, "
                             f"got {data_rate!r}")
        if not 1 <= payload_size <= 32:
            raise ValueError(f"payload_size must be 1..32, got {payload_size}")
        address = bytes(address)
        if len(address) != _ADDR_LEN:
            raise ValueError(f"address must be {_ADDR_LEN} bytes, got {len(address)}")

        self._spi = bridge.spi
        self._gpio = bridge.gpio
        self._cs = cs
        self._ce_pin = ce
        self._payload = payload_size

        # SPI: mode 0, MSB-first (CPOL=0/CPHA=0, the nRF24's required mode).
        if sck is not None or miso is not None or mosi is not None:
            pins = {k: v for k, v in (("sck", sck), ("miso", miso), ("mosi", mosi))
                    if v is not None}
            self._spi.init(freq=freq, mode=0, msb_first=True, **pins)

        # CE low (idle/standby), CS high idle. CS is actually driven by the
        # firmware during each transfer (cs=self._cs); we still park it high so
        # the line is defined before the first frame.
        self._gpio.mode(ce, "output")
        self._gpio.write(ce, 0)
        self._gpio.mode(cs, "output")
        self._gpio.write(cs, 1)

        # Power up as a CRC-enabled transmitter. 2-byte CRC (EN_CRC|CRCO),
        # PWR_UP set. PRIM_RX=0 -> PTX. Datasheet: ~1.5 ms power-up to standby.
        self._config = _EN_CRC | _CRCO | _PWR_UP
        self._write_reg(_CONFIG, self._config)
        time.sleep(0.005)

        self.set_channel(channel)
        self._write_reg(_RF_SETUP, _DATA_RATES[data_rate] | _RF_PWR_0DBM)

        # Same address on TX_ADDR and RX pipe 0 so auto-ACK works (the PTX
        # listens on pipe 0 for the ACK from the PRX with the matching address).
        self._write_reg(_TX_ADDR, address)
        self._write_reg(_RX_ADDR_P0, address)
        self._write_reg(_RX_PW_P0, payload_size)
        self._write_reg(_EN_RXADDR, 0x01)   # enable pipe 0
        self._write_reg(_EN_AA, 0x01)       # auto-ack on pipe 0
        self._write_reg(_SETUP_AW, 0b11)    # 5-byte addresses
        # ARD = 1500us (0xF), ARC = 15 retries — generous defaults.
        self._write_reg(_SETUP_RETR, 0xFF)

        self._command(_FLUSH_TX)
        self._command(_FLUSH_RX)
        # Clear any latched interrupt flags (write 1 to clear).
        self._write_reg(_STATUS, _RX_DR | _TX_DS | _MAX_RT)

    # -- low-level SPI helpers ------------------------------------------------
    # Every SPI access on the nRF24 returns the STATUS byte on the first MISO
    # byte while the command is clocked in (PS v1.0, sec. 8.3.1). CS must stay
    # low for the whole command+data frame — bridge.spi.transfer(cs=...) does
    # exactly that.

    def _read_reg(self, reg: int, n: int = 1) -> bytes:
        """Read n bytes from register `reg`; returns the register data only."""
        rx = self._spi.transfer(bytes([_R_REGISTER | reg]) + bytes(n), cs=self._cs)
        return rx[1:]               # drop the STATUS byte clocked out first

    def _write_reg(self, reg: int, data) -> int:
        """Write `data` (int or bytes) to register `reg`; returns STATUS."""
        if isinstance(data, int):
            data = bytes([data])
        else:
            data = bytes(data)
        rx = self._spi.transfer(bytes([_W_REGISTER | reg]) + data, cs=self._cs)
        return rx[0]

    def _command(self, cmd: int) -> int:
        """Send a single-byte command (FLUSH_TX/RX, NOP); returns STATUS."""
        return self._spi.transfer(bytes([cmd]), cs=self._cs)[0]

    def _status(self) -> int:
        """Read the STATUS register (NOP clocks it out without side effects)."""
        return self._command(_NOP)

    def _set_prim_rx(self, rx: bool) -> None:
        if rx:
            self._config |= _PRIM_RX
        else:
            self._config &= ~_PRIM_RX
        self._write_reg(_CONFIG, self._config)

    def _ce(self, level: int) -> None:
        self._gpio.write(self._ce_pin, level)

    # -- public API -----------------------------------------------------------

    def set_channel(self, ch: int) -> None:
        """Set the RF channel (0..125 -> 2400..2525 MHz, 1 MHz steps)."""
        if not 0 <= ch <= 125:
            raise ValueError(f"channel must be 0..125, got {ch}")
        self._write_reg(_RF_CH, ch)

    def send(self, data: bytes, *, timeout: float = 0.5) -> bool:
        """Transmit `data` and block for the ACK. Returns True on TX_DS
        (acknowledged), False if the auto-retransmit count was exhausted
        (MAX_RT). `data` is truncated/zero-padded to the configured
        payload_size."""
        data = bytes(data)
        if len(data) > self._payload:
            data = data[: self._payload]
        elif len(data) < self._payload:
            data = data + b"\x00" * (self._payload - len(data))

        self.stop_listening()                 # CE low + PTX mode
        self._set_prim_rx(False)
        self._command(_FLUSH_TX)
        self._write_reg(_STATUS, _TX_DS | _MAX_RT)   # clear stale flags

        self._spi.transfer(bytes([_W_TX_PAYLOAD]) + data, cs=self._cs)

        # Pulse CE high >=10us to start the transmission, then drop it.
        self._ce(1)
        time.sleep(0.00002)                   # ~20us, comfortably over the 10us min
        self._ce(0)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self._status()
            if status & _TX_DS:
                self._write_reg(_STATUS, _TX_DS)
                return True
            if status & _MAX_RT:
                self._write_reg(_STATUS, _MAX_RT)
                self._command(_FLUSH_TX)
                return False
            time.sleep(0.0005)
        # Timed out without either flag — clear what may be pending and report
        # failure rather than leaving the radio wedged.
        self._command(_FLUSH_TX)
        self._write_reg(_STATUS, _TX_DS | _MAX_RT)
        return False

    def start_listening(self) -> None:
        """Enter PRX (receive) mode and raise CE to start sampling the air."""
        self._set_prim_rx(True)
        self._write_reg(_STATUS, _RX_DR | _TX_DS | _MAX_RT)
        self._command(_FLUSH_RX)
        self._ce(1)
        time.sleep(0.00013)                   # RX settling ~130us before data is valid

    def stop_listening(self) -> None:
        """Drop CE — leave RX mode and return to standby."""
        self._ce(0)

    def available(self) -> bool:
        """True if a payload is waiting: RX_DR latched in STATUS, or the RX
        FIFO is non-empty per FIFO_STATUS."""
        if self._status() & _RX_DR:
            return True
        return not (self._read_reg(_FIFO_STATUS)[0] & _RX_EMPTY)

    def read(self) -> bytes:
        """Read one payload (payload_size bytes) from the RX FIFO and clear
        the RX_DR flag."""
        rx = self._spi.transfer(bytes([_R_RX_PAYLOAD]) + bytes(self._payload),
                                cs=self._cs)
        self._write_reg(_STATUS, _RX_DR)
        return rx[1:]               # drop the leading STATUS byte

    def receive(self, timeout: float | None = None) -> bytes | None:
        """Listen for one packet and return it, or None on timeout.

        Mirrors the LoRa drivers' ``send()``/``receive()`` API: enters RX, waits
        up to ``timeout`` seconds (``None`` = until a packet arrives), returns the
        payload, then stops listening. For continuous RX use
        ``start_listening()`` / ``available()`` / ``read()`` directly."""
        self.start_listening()
        try:
            deadline = None if timeout is None else time.monotonic() + timeout
            while not self.available():
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                time.sleep(0.001)
            return self.read()
        finally:
            self.stop_listening()
