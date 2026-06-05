"""smbus2-style I2C — paste-compatible with classic Raspberry Pi code.

Scans the bus, then reads one register from a device (default: the first
device found, register 0xD0 — the BME280 chip-id, harmless on most chips):

    uv run smbus/read_register.py               # auto-pick first device
    uv run smbus/read_register.py 0x76 0xD0     # explicit address/register

Wiring: SDA -> GPIO21, SCL -> GPIO22.
"""
import sys

from espbridge import Bridge
from espbridge.compat.smbus import SMBus

ADDR = int(sys.argv[1], 0) if len(sys.argv) > 1 else None
REGISTER = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0xD0

with Bridge() as esp:
    bus = SMBus(esp)

    devices = esp.i2c.scan()
    if not devices:
        raise SystemExit("no I2C devices found — check wiring (SDA=GPIO21, "
                         "SCL=GPIO22) and power")
    print("devices on bus:", ", ".join(f"0x{a:02X}" for a in devices))

    addr = ADDR if ADDR is not None else devices[0]
    if addr not in devices:
        raise SystemExit(f"nothing at 0x{addr:02X} — pick one of the above")

    value = bus.read_byte_data(addr, REGISTER)  # unchanged smbus2 API from here
    print(f"device 0x{addr:02X}, register 0x{REGISTER:02X} = 0x{value:02X}")
