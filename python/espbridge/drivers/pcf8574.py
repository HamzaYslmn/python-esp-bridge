"""PCF8574 / PCF8574A — 8-bit I2C GPIO expander.

A worked example of a "bring your own" driver: a plain class over the bridge's
I2C primitive, ~50 lines, no firmware change. Copy it as a starting point for
your own device (see docs/DRIVERS.md).

    from espbridge import Bridge

    with Bridge() as esp:
        io = esp.pcf8574(address=0x20)   # == PCF8574(esp, address=0x20)
        io.write_port(0b0000_0001)       # drive P0 high, P1..P7 low
        io.write_pin(7, 0)               # pull P7 low (read-modify-write)
        print(io.read_pin(2))            # sample an input

The chip has no direction register: a pin reads as an input only while it is
driven high (the weak internal pull-up), so configure inputs by writing 1 to
them. Addresses are 0x20-0x27 (PCF8574) or 0x38-0x3F (PCF8574A), set by the
A0..A2 strap pins.
"""
from __future__ import annotations

from ..i2c import bind_i2c


class PCF8574:
    def __init__(self, bridge, address: int = 0x20, *, bus: int = 0,
                 sda: int | None = None, scl: int | None = None):
        if not 0x20 <= address <= 0x3F:
            raise ValueError(f"PCF8574 address {address:#04x} out of range "
                             f"(0x20-0x27 or 0x38-0x3F)")
        self._i2c, self._addr, self._bus = bind_i2c(bridge, address, bus=bus, sda=sda, scl=scl)
        # Power-on state of the latch is all-high (all pins usable as inputs).
        self._state = 0xFF

    def write_port(self, value: int) -> None:
        """Write all 8 pins at once (bit 0 = P0 ... bit 7 = P7)."""
        self._state = value & 0xFF
        self._i2c.write(self._addr, bytes([self._state]), self._bus)

    def read_port(self) -> int:
        """Read all 8 pins as a byte. Pins to be sampled must be left high."""
        return self._i2c.read(self._addr, 1, self._bus)[0]

    def write_pin(self, pin: int, level: int) -> None:
        """Set one pin without disturbing the others (read-modify-write the latch)."""
        self._check(pin)
        if level:
            self._state |= 1 << pin
        else:
            self._state &= ~(1 << pin)
        self.write_port(self._state)

    def read_pin(self, pin: int) -> int:
        """Sample one pin (0 or 1). The pin must be left driven high to read it."""
        self._check(pin)
        return (self.read_port() >> pin) & 1

    @staticmethod
    def _check(pin: int) -> None:
        if not 0 <= pin <= 7:
            raise ValueError(f"PCF8574 pin must be 0..7, got {pin}")
