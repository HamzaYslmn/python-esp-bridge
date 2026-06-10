"""Write your own device driver — pure Python, no firmware change.

The firmware only knows hardware primitives (I2C, SPI, UART, RMT, ...); a
"driver" is any class that takes the Bridge as its first constructor argument
and speaks the device's protocol over them. This file is the whole recipe,
using an LM75/TMP102-style I2C thermometer (any breakout at 0x48):

    uv run devices/custom_driver.py

Full guide (incl. shipping your driver as a pip plugin): docs/DRIVERS.md.
"""
from espbridge import Bridge, register_driver


class TempSensor:
    """LM75/TMP102 thermometer. The one contract: Bridge comes first."""

    def __init__(self, bridge, address: int = 0x48, *, bus: int = 0):
        self._i2c = bridge.i2c        # drivers use the primitives, never the wire
        self._addr = address
        self._bus = bus

    def read_c(self) -> float:
        # register 0x00: big-endian 12-bit temperature in 1/16 degC steps
        hi, lo = self._i2c.read_reg(self._addr, 0x00, 2, self._bus)
        raw = (hi << 8 | lo) >> 4
        if raw & 0x800:               # negative temperatures are two's complement
            raw -= 0x1000
        return raw * 0.0625


# Optional: register a name so any Bridge grows an `esp.lm75(...)` factory —
# this is the same mechanism the bundled drivers (esp.dht, esp.oled, ...) use.
register_driver("lm75", TempSensor)


def main() -> None:
    with Bridge() as esp:
        esp.i2c.init(sda=21, scl=22)

        if 0x48 not in esp.i2c.scan():
            print("no sensor at 0x48 — wire an LM75/TMP102 breakout to 21/22")
            return

        # Both spellings build the same object:
        plain = TempSensor(esp)             # plain class, no registration needed
        bound = esp.lm75()                  # via register_driver("lm75", ...)
        print(f"temperature: {plain.read_c():.2f} degC "
              f"(and via esp.lm75(): {bound.read_c():.2f} degC)")


if __name__ == "__main__":
    main()
