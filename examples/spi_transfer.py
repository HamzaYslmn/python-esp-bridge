"""SPI: read the JEDEC ID of a flash chip (CS on GPIO 5).

Works with any SPI flash (W25Qxx etc.); adapt to your own SPI device.
"""
from espbridge import Bridge

with Bridge() as esp:
    esp.spi.init(sck=18, miso=19, mosi=23, freq=1_000_000, mode=0)
    rx = esp.spi.transfer(bytes([0x9F, 0, 0, 0]), cs=5)  # JEDEC Read ID
    mfg, mem_type, capacity = rx[1], rx[2], rx[3]
    print(f"JEDEC ID: mfg=0x{mfg:02X} type=0x{mem_type:02X} capacity=0x{capacity:02X}")
