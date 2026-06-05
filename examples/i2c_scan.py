"""Scan the I2C bus (SDA=21, SCL=22 — the classic DevKit defaults)."""
from espbridge import Bridge

with Bridge() as esp:
    esp.i2c.init(sda=21, scl=22, freq=400_000)
    addrs = esp.i2c.scan()
    if not addrs:
        print("no I2C devices found")
    for a in addrs:
        print(f"found device at 0x{a:02X}")
