"""OTA: chunked image transfer, progress, partition-scheme error."""
import math

import pytest

from espbridge import constants as C
from espbridge.errors import UnsupportedError


def image(n: int = 4096) -> bytes:
    return b"\xe9" + bytes((i * 7) & 0xFF for i in range(n - 1))


def test_flash_roundtrip(bridge, fw):
    data = image(5000)
    ticks = []
    written = bridge.ota.flash(data, progress=lambda d, t: ticks.append((d, t)))
    assert written == 5000
    assert bytes(fw.ota_data) == data
    assert fw.ota_size == 5000
    assert fw.ota_committed is True
    assert ticks[-1] == (5000, 5000)
    assert len(ticks) == math.ceil(5000 / C.OTA_CHUNK)  # one tick per chunk


def test_flash_no_reboot(bridge, fw):
    bridge.ota.flash(image(), reboot=False)
    assert fw.ota_committed is False


def test_rejects_non_image(bridge):
    with pytest.raises(ValueError):
        bridge.ota.flash(b"\x00" * 4096)
    with pytest.raises(ValueError):
        bridge.ota.flash(b"\xe9 too short")


def test_no_ota_partition_hint(bridge, fw):
    fw.ota_has_partition = False
    with pytest.raises(UnsupportedError, match="Minimal SPIFFS"):
        bridge.ota.flash(image())


def test_flash_from_file(bridge, fw, tmp_path):
    path = tmp_path / "fw.bin"
    path.write_bytes(image(2048))
    assert bridge.ota.flash(path) == 2048
