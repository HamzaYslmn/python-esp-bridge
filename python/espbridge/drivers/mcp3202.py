"""MCP3202 — 12-bit, 2-channel SPI analog-to-digital converter (Microchip).

A "bring your own" driver over the bridge's SPI primitive: single-ended (or
differential) single-shot reads, decoded to a 0..4095 code or to volts. No
firmware change needed. Same 12-bit result decode as the MCP3208 — only the
command frame and the channel count (2) differ.

    from espbridge import Bridge

    with Bridge() as esp:
        esp.spi.init(sck=18, miso=19, mosi=23, freq=1_600_000)
        adc = esp.mcp3202(cs=5, vref=3.3)
        code = adc.read(0)            # 0..4095 on CH0
        v    = adc.read_voltage(0)    # volts on CH0

Wiring: MCP3202 is SPI mode 0. Clock ceiling is ~1.8 MHz at a 3.3 V supply
(3.2 MHz only at 5 V) — keep esp.spi.init(freq=) at or below that. VREF sets
full scale; tie it to the supply you measure against and pass the same `vref`.

Protocol (3 bytes, datasheet DS21034 sec. 6.1, MSB-first):
    tx = [0x01, 0xA0 | (ch << 6), 0x00]
         byte0 = start bit (LSB)
         byte1 bit7 = SGL/DIFF (1 = single-ended), bit6 = ODD/SIGN (channel),
               bit5 = MSBF (1)
    code = ((rx[1] & 0x0F) << 8) | rx[2]   # 12-bit result in the tail two bytes
"""
from __future__ import annotations


class MCP3202:
    def __init__(self, bridge, *, cs: int, vref: float = 3.3, host: int = 0):
        self._spi = bridge.spi
        self._cs = cs
        self._vref = vref
        self._host = host

    def read(self, channel: int, *, differential: bool = False) -> int:
        """Sample one channel -> 12-bit code (0..4095).

        Single-ended (default): `channel` 0 or 1 is CHn vs GND. Differential:
        0 = CH0(IN+)/CH1(IN-), 1 = CH1(IN+)/CH0(IN-).
        """
        if channel not in (0, 1):
            raise ValueError(f"MCP3202 channel must be 0 or 1, got {channel}")
        sgl = 0 if differential else 1
        b1 = (sgl << 7) | (channel << 6) | (1 << 5)   # SGL/DIFF, ODD/SIGN, MSBF
        rx = self._spi.transfer(bytes([0x01, b1, 0x00]), cs=self._cs, host=self._host)
        return ((rx[1] & 0x0F) << 8) | rx[2]

    def read_voltage(self, channel: int, *, differential: bool = False) -> float:
        """Sample one channel -> volts (code / 4096 * vref)."""
        return self.read(channel, differential=differential) / 4096.0 * self._vref
