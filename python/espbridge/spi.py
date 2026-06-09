"""SPI master, full-duplex transfers."""
from __future__ import annotations

import struct

from . import constants as C

# Maximum data bytes per TRANSFER request: total payload cap minus the 2-byte header (host index + CS pin).
_CHUNK = C.MAX_PAYLOAD - 2


class Spi:
    """SPI master, full-duplex.

        esp.spi.init(sck=18, miso=19, mosi=23, freq=1_000_000)
        rx = esp.spi.transfer(b"\\x9f\\x00\\x00", cs=5)  # read JEDEC ID
    """

    def __init__(self, bridge):
        self._b = bridge

    def init(self, *, sck: int = 18, miso: int = 19, mosi: int = 23,
             freq: int = 1_000_000, mode: int = 0, msb_first: bool = True,
             host: int = 0) -> None:
        """Configure the SPI host's pins, clock and mode; call once first."""
        self._b.request(C.SPI_INIT, struct.pack(">BbbbIBB", host, sck, miso, mosi,
                                                freq, mode, 1 if msb_first else 0))

    def transfer(self, tx: bytes, *, cs: int | None = None, host: int = 0) -> bytes:
        """Full-duplex transfer; returns the bytes clocked in (len == len(tx)).

        With `cs` given, CS is held low for the whole transfer — limited to
        one frame (≤ 2046 bytes). Without `cs`, any length (chunked).
        """
        tx = bytes(tx)
        cs_b = 255 if cs is None else cs  # firmware treats 255 (0xFF) as "no CS"; equivalent to signed -1
        if cs is not None and len(tx) > _CHUNK:
            raise ValueError(
                f"transfers with CS are limited to {_CHUNK} bytes per call "
                "(CS would deassert between chunks); split the transfer or manage "
                "CS manually via gpio"
            )
        rx = bytearray()
        for off in range(0, len(tx), _CHUNK):
            chunk = tx[off : off + _CHUNK]
            rx += self._b.request(C.SPI_TRANSFER, bytes([host, cs_b]) + chunk, timeout=10.0)
        return bytes(rx)

    def deinit(self, host: int = 0) -> None:
        """Release the SPI host and its pins on the firmware."""
        self._b.request(C.SPI_DEINIT, bytes([host]))

    # House style: begin()/end() work on every peripheral (Arduino-friendly).
    begin = init
    end = deinit
