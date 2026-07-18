"""MCP3202 driver — decode verified against an in-process chip emulator.

No hardware: a fake SPI backend answers exactly like an MCP3202 would, so the
command framing and 12-bit decode are exercised end to end.
"""
from espbridge.drivers.mcp3202 import MCP3202


class FakeMCP3202SPI:
    """Emulates the MCP3202 side of a full-duplex 3-byte SPI transfer."""

    def __init__(self, codes):
        self.codes = codes      # channel -> 12-bit code the chip should report
        self.last = None        # (channel, differential) decoded from the command

    def transfer(self, tx, *, cs=None, host=0):
        assert len(tx) == 3, "MCP3202 read is always 3 bytes"
        assert tx[0] == 0x01, "byte0 must carry the start bit"
        single = (tx[1] >> 7) & 1
        ch = (tx[1] >> 6) & 1
        assert (tx[1] >> 5) & 1, "MSBF must be set"
        self.last = (ch, not single)
        code = self.codes.get(ch, 0) & 0x0FFF
        return bytes([0x00, code >> 8, code & 0xFF])


class FakeBridge:
    def __init__(self, spi):
        self.spi = spi


def _adc(codes):
    spi = FakeMCP3202SPI(codes)
    return MCP3202(FakeBridge(spi), cs=5, vref=3.3), spi


def test_channel_framing_and_decode():
    adc, spi = _adc({0: 1234, 1: 4095})
    assert adc.read(0) == 1234
    assert spi.last == (0, False)
    assert adc.read(1) == 4095
    assert spi.last == (1, False)


def test_full_scale_voltage():
    adc, _ = _adc({1: 4095})
    assert abs(adc.read_voltage(1) - 4095 / 4096 * 3.3) < 1e-9


def test_differential_flag():
    adc, spi = _adc({0: 42})
    assert adc.read(0, differential=True) == 42
    assert spi.last == (0, True)


def test_channel_range():
    adc, _ = _adc({})
    for bad in (-1, 2, 8):
        try:
            adc.read(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"channel {bad} should raise")
