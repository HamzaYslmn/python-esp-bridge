"""MCP3208 driver — decode verified against an in-process chip emulator.

No hardware: a fake SPI backend answers exactly like an MCP3208 would, so the
command framing and 12-bit decode are exercised end to end.
"""
from espbridge.drivers.mcp3208 import MCP3208


class FakeMCP3208SPI:
    """Emulates the MCP3208 side of a full-duplex 3-byte SPI transfer."""

    def __init__(self, codes):
        self.codes = codes      # channel -> 12-bit code the chip should report
        self.last = None        # (channel, differential) decoded from the command

    def transfer(self, tx, *, cs=None, host=0):
        assert len(tx) == 3, "MCP3208 read is always 3 bytes"
        single = (tx[0] >> 1) & 1
        ch = ((tx[0] & 1) << 2) | (tx[1] >> 6)   # D2 from byte0, D1D0 from byte1
        self.last = (ch, not single)
        code = self.codes.get(ch, 0) & 0x0FFF
        # byte0 clocked out during command bits = don't-care; result in the tail.
        return bytes([0x00, code >> 8, code & 0xFF])


class FakeBridge:
    def __init__(self, spi):
        self.spi = spi


def _adc(codes):
    spi = FakeMCP3208SPI(codes)
    return MCP3208(FakeBridge(spi), cs=5, vref=3.3), spi


def test_channel_framing_and_decode():
    adc, spi = _adc({c: c * 500 for c in range(8)})
    for ch in range(8):
        assert adc.read(ch) == ch * 500
        assert spi.last == (ch, False)


def test_full_scale_voltage():
    adc, _ = _adc({0: 4095})
    assert abs(adc.read_voltage(0) - 4095 / 4096 * 3.3) < 1e-9


def test_differential_flag():
    adc, spi = _adc({3: 100})
    assert adc.read(3, differential=True) == 100
    assert spi.last == (3, True)


def test_channel_range():
    adc, _ = _adc({})
    for bad in (-1, 8):
        try:
            adc.read(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"channel {bad} should raise")
