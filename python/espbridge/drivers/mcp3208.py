"""MCP3208 — 12-bit, 8-channel SPI analog-to-digital converter (Microchip).

A "bring your own" driver over the bridge's SPI primitive: single-ended (or
differential) single-shot reads, decoded to a 0..4095 code or to volts. No
firmware change needed.

    from espbridge import Bridge

    with Bridge() as esp:
        esp.spi.init(sck=18, miso=19, mosi=23, freq=1_000_000)
        adc = esp.mcp3208(cs=5, vref=3.3)
        code = adc.read(0)            # 0..4095 on CH0
        v    = adc.read_voltage(0)    # volts on CH0

Wiring: MCP3208 is SPI mode 0. At a 3.3 V supply the datasheet clock ceiling is
~1 MHz (2 MHz only at 5 V) — keep esp.spi.init(freq=) at or below that or the
low bits get noisy. VREF sets full scale; tie it to the supply you measure
against and pass the same value as `vref`.

Protocol (3 bytes, datasheet DS21298 sec. 6.1, single-ended):
    tx = [0b0000_0110 | (ch >> 2), (ch & 3) << 6, 0x00]
         byte0 bit2 = start, bit1 = SGL/DIFF (1 = single-ended), bit0 = D2
         byte1 bits7:6 = D1 D0
    code = ((rx[1] & 0x0F) << 8) | rx[2]   # 12-bit result in the tail two bytes
"""
from __future__ import annotations


class MCP3208:
    def __init__(self, bridge, *, cs: int, vref: float = 3.3, host: int = 0):
        self._spi = bridge.spi
        self._cs = cs
        self._vref = vref
        self._host = host

    def read(self, channel: int, *, differential: bool = False) -> int:
        """Sample one channel -> 12-bit code (0..4095).

        Single-ended (default): `channel` 0..7 is CHn vs AGND. Differential:
        `channel` 0..7 selects a CH+/CH- pair (0 = CH0-CH1, 1 = CH1-CH0, ...
        datasheet Table 5-2).
        """
        if not 0 <= channel <= 7:
            raise ValueError(f"MCP3208 channel must be 0..7, got {channel}")
        sgl = 0 if differential else 1
        b0 = (1 << 2) | (sgl << 1) | (channel >> 2)
        b1 = (channel & 0x03) << 6
        rx = self._spi.transfer(bytes([b0, b1, 0x00]), cs=self._cs, host=self._host)
        return ((rx[1] & 0x0F) << 8) | rx[2]

    def read_voltage(self, channel: int, *, differential: bool = False) -> float:
        """Sample one channel -> volts (code / 4096 * vref)."""
        return self.read(channel, differential=differential) / 4096.0 * self._vref
