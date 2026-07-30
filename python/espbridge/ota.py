"""Firmware update over the protocol link — USB serial or Bluetooth.

    esp.ota.flash("Bridge.ino.bin",
                  progress=lambda done, total: print(f"{done}/{total}"))

Needs a partition table with two app slots: in Arduino IDE pick
Tools > Partition Scheme > "Minimal SPIFFS (1.9MB APP with OTA)" and flash
once over USB; every later update can go over the air. Rough speed:
~20 s per MB over USB at 921600 baud, 1-4 min per MB over BLE.
"""
from __future__ import annotations

import struct
from pathlib import Path
from collections.abc import Callable

from . import constants as C
from .errors import RemoteError, needs_fw
from .protocol import chunks


class Ota:
    """Over-the-air firmware updates over the protocol link.

        esp.ota.flash("Bridge.ino.bin",
                      progress=lambda done, total: print(f"{done}/{total}"))
        esp.ota.abort()   # cancel an in-progress flash
    """

    def __init__(self, bridge):
        self._b = bridge
        bridge.require(C.Cap.OTA, "OTA")

    def flash(self, firmware: bytes | str | Path, *,
              progress: Callable[[int, int], None] | None = None,
              reboot: bool = True) -> int:
        """Write a compiled firmware image (.bin) to the inactive app slot.

        Returns bytes written. With reboot=True (default) the board restarts
        into the new firmware and re-announces itself (SYS_READY).
        """
        data = Path(firmware).read_bytes() if isinstance(firmware, (str, Path)) else firmware
        if len(data) < 1024 or data[0] != 0xE9:  # ESP32 image magic
            raise ValueError("not an ESP32 firmware image (.bin)")
        try:
            self._b.request(C.OTA_BEGIN, struct.pack(">I", len(data)), timeout=15.0)
        except RemoteError as e:
            needs_fw(e, "no OTA partition — flash once over USB with a dual-app "
                        "scheme (Arduino: Partition Scheme > 'Minimal SPIFFS')",
                     status=C.Status.UNSUPPORTED)
        written = 0
        for chunk in chunks(data, C.OTA_CHUNK):
            r = self._b.request(C.OTA_WRITE, chunk, timeout=15.0)
            written = struct.unpack(">I", r)[0]
            if progress:
                progress(written, len(data))
        self._b.request(C.OTA_END, bytes([1 if reboot else 0]), timeout=30.0)
        return written

    def abort(self) -> None:
        """Cancel an in-progress OTA update and discard the partial image."""
        self._b.request(C.OTA_ABORT)
