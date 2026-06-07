"""Camera — JPEG snapshots from OV-series sensors (firmware opt-in).

Build the firmware with ``#define BRIDGE_ENABLE_CAM 1`` (esp32/s2/s3 with
PSRAM), then:

    esp.camera.begin("ai-thinker")            # ESP32-CAM board
    jpeg = esp.camera.capture()               # bytes, ready to save
    open("shot.jpg", "wb").write(jpeg)
    esp.camera.set("framesize", FRAMESIZE_VGA)

Frames cross the link at ~92 KB/s over USB — snapshots, not video.
"""
from __future__ import annotations

import struct

from . import constants as C

# framesize_t values (esp32-camera)
FRAMESIZE_QQVGA, FRAMESIZE_QCIF, FRAMESIZE_HQVGA, FRAMESIZE_240X240, \
    FRAMESIZE_QVGA, FRAMESIZE_CIF, FRAMESIZE_HVGA, FRAMESIZE_VGA, \
    FRAMESIZE_SVGA, FRAMESIZE_XGA, FRAMESIZE_HD, FRAMESIZE_SXGA, \
    FRAMESIZE_UXGA = range(13)

# CAM_SET property ids (mirrors firmware mod_cam.cpp)
_PROPS = {"framesize": 0, "quality": 1, "brightness": 2, "contrast": 3,
          "saturation": 4, "vflip": 5, "hmirror": 6, "special_effect": 7,
          "whitebal": 8, "exposure_ctrl": 9, "aec_value": 10,
          "gain_ctrl": 11, "agc_gain": 12}

# pin maps: pwdn, reset, xclk, siod, sioc, d7..d0, vsync, href, pclk
PRESETS: dict[str, list[int]] = {
    "ai-thinker":    [32, -1, 0, 26, 27, 35, 34, 39, 36, 21, 19, 18, 5, 25, 23, 22],
    "esp-eye":       [-1, -1, 4, 18, 23, 36, 37, 38, 39, 35, 14, 13, 34, 5, 27, 25],
    "m5stack-wide":  [-1, 15, 27, 22, 23, 19, 36, 18, 39, 5, 34, 35, 32, 25, 26, 21],
    "xiao-s3-sense": [-1, -1, 10, 40, 39, 48, 11, 12, 14, 16, 18, 17, 15, 38, 47, 13],
    "freenove-s3":   [-1, -1, 15, 4, 5, 16, 17, 18, 12, 10, 8, 9, 11, 6, 7, 13],
}

_CHUNK = C.MAX_PAYLOAD - 8


class Camera:
    def __init__(self, bridge):
        self._b = bridge
        bridge.require(C.Cap.CAM, "camera (BRIDGE_ENABLE_CAM=1 firmware + PSRAM)")

    def begin(self, preset: str | list[int] = "ai-thinker", *,
              framesize: int = FRAMESIZE_VGA, quality: int = 12,
              xclk_mhz: int = 20, fb_count: int = 1) -> None:
        """Init the sensor. preset = board name or a 16-entry pin list.
        quality: JPEG 0 (best) .. 63; fb_count 2 double-buffers in PSRAM."""
        pins = PRESETS[preset] if isinstance(preset, str) else list(preset)
        if len(pins) != 16:
            raise ValueError("pin map must have 16 entries")
        payload = struct.pack(">16b", *pins) + bytes([xclk_mhz, framesize,
                                                      quality, fb_count])
        self._b.request(C.CAM_INIT, payload, timeout=15.0)

    def capture(self) -> bytes:
        """Grab one frame and pull it across the link (JPEG bytes)."""
        r = self._b.request(C.CAM_CAPTURE, timeout=10.0)
        total = struct.unpack(">I", r[:4])[0]
        out = bytearray(total)  # size known up front: fill by slice, no O(n^2) growth
        pos = 0
        while pos < total:
            chunk = self._b.request(
                C.CAM_READ, struct.pack(">IH", pos, min(_CHUNK, total - pos)))
            if not chunk:
                break
            out[pos:pos + len(chunk)] = chunk
            pos += len(chunk)
        self._b.request(C.CAM_RELEASE)
        return bytes(out[:pos])

    def set(self, prop: str, value: int) -> None:
        """Tune the sensor: framesize, quality, brightness (-2..2),
        contrast, saturation, vflip, hmirror, ... (see _PROPS)."""
        self._b.request(C.CAM_SET, struct.pack(">Bi", _PROPS[prop], value))

    def end(self) -> None:
        self._b.request(C.CAM_DEINIT)
