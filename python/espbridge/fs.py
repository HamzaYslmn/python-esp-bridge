"""Files on the ESP32: LittleFS (internal flash) and SD cards.

    lfs = esp.fs.mount("littlefs")
    lfs.write_file("/hello.txt", b"hi there")
    print(lfs.read_file("/hello.txt"))
    for name, size, isdir in lfs.list("/"):
        print(name, size)

    sd = esp.fs.mount("sd", cs=5)              # SD card over SPI
    mmc = esp.fs.mount("sdmmc")                # SDMMC slot (esp32/s3)
"""
from __future__ import annotations

import struct

from . import constants as C
from .protocol import lp, CHUNK, chunks

_IDS = {"littlefs": 0, "sd": 1, "sdmmc": 2}
_MODES = {"r": 0, "w": 1, "a": 2}


class RemoteFile:
    """File handle on the ESP32 — read/write/seek/close, context manager."""

    def __init__(self, bridge, fd: int, size: int):
        self._b = bridge
        self._fd = fd
        self.size = size
        self._open = True

    def read(self, n: int | None = None) -> bytes:
        """Read n bytes (whole file when n is None)."""
        parts: list[bytes] = []
        got = 0
        while n is None or got < n:
            want = CHUNK if n is None else min(CHUNK, n - got)
            chunk = self._b.request(C.FS_READ, struct.pack(">BH", self._fd, want))
            parts.append(chunk)
            got += len(chunk)
            if len(chunk) < want:  # EOF
                break
        return b"".join(parts)

    def write(self, data: bytes) -> int:
        """Write `data` at the current position; returns bytes written.

        Raises OSError on a short write (e.g. filesystem full).
        """
        written = 0
        for chunk in chunks(data):
            r = self._b.request(C.FS_WRITE, bytes([self._fd]) + chunk)
            w = struct.unpack(">H", r)[0]
            written += w
            if w < len(chunk):
                raise OSError("short write (filesystem full?)")
        return written

    def seek(self, pos: int) -> None:
        """Move the read/write cursor to absolute byte offset `pos`."""
        self._b.request(C.FS_SEEK, struct.pack(">BI", self._fd, pos))

    def close(self) -> None:
        """Flush and close the file handle (idempotent)."""
        if self._open:
            self._open = False
            self._b.request(C.FS_CLOSE, bytes([self._fd]))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Volume:
    """One mounted filesystem (returned by ``esp.fs.mount``). Paths are absolute.

        >>> vol = esp.fs.mount("littlefs")
        >>> vol.write_file("/a.txt", b"hi")
        >>> vol.read_file("/a.txt")
        b'hi'
        >>> for name, size, isdir in vol.list("/"):
        ...     print(name, size, isdir)
        >>> vol.umount()
    """

    def __init__(self, bridge, fs_id: int, total_kb: int, used_kb: int):
        self._b = bridge
        self._id = fs_id
        self.total_kb = total_kb
        self.used_kb = used_kb

    def _path(self, path: str) -> bytes:
        if not path.startswith("/"):
            raise ValueError("paths must be absolute ('/...')")
        return bytes([self._id]) + path.encode()

    def open(self, path: str, mode: str = "r") -> RemoteFile:
        """Open `path` and return a RemoteFile. mode: 'r', 'w' or 'a'."""
        r = self._b.request(C.FS_OPEN,
                            bytes([self._id, _MODES[mode]]) + self._path(path)[1:])
        fd, size = struct.unpack(">BI", r)
        return RemoteFile(self._b, fd, size)

    def read_file(self, path: str) -> bytes:
        """Read and return the whole file at `path`."""
        with self.open(path) as f:
            return f.read()

    def write_file(self, path: str, data: bytes) -> None:
        """Write `data` to `path`, truncating any existing file."""
        with self.open(path, "w") as f:
            f.write(data)

    def append_file(self, path: str, data: bytes) -> None:
        """Append `data` to `path` (creating it if needed)."""
        with self.open(path, "a") as f:
            f.write(data)

    def list(self, path: str = "/") -> list[tuple[str, int, bool]]:
        """-> [(name, size, isdir), ...]"""
        entries: list[tuple[str, int, bool]] = []

        def on_entry(payload: bytes) -> None:
            isdir, size = payload[0], struct.unpack_from(">I", payload, 1)[0]
            entries.append((payload[5:].decode(), size, bool(isdir)))

        self._b.on_event(C.FS_LIST_EVT, on_entry)
        try:
            self._b.request(C.FS_LIST, self._path(path))
        finally:
            self._b.off_event(C.FS_LIST_EVT, on_entry)
        return entries

    def stat(self, path: str) -> tuple[int, bool, int]:
        """-> (size, isdir, mtime unix)"""
        r = self._b.request(C.FS_STAT, self._path(path))
        size, isdir, mtime = struct.unpack(">IBI", r)
        return size, bool(isdir), mtime

    def remove(self, path: str) -> None:
        """Delete the file or empty directory at `path`."""
        self._b.request(C.FS_REMOVE, self._path(path))

    def rename(self, src: str, dst: str) -> None:
        """Rename/move `src` to `dst` (both absolute paths)."""
        if not (src.startswith("/") and dst.startswith("/")):
            raise ValueError("paths must be absolute ('/...')")
        self._b.request(C.FS_RENAME,
                        bytes([self._id]) + lp(src) + dst.encode())

    def mkdir(self, path: str) -> None:
        """Create a directory at `path`."""
        self._b.request(C.FS_MKDIR, self._path(path))

    def df(self) -> tuple[int, int]:
        """-> (total_kb, used_kb)"""
        r = self._b.request(C.FS_DF, bytes([self._id]))
        return struct.unpack(">II", r)

    def umount(self) -> None:
        """Unmount this filesystem; the Volume must not be used afterward."""
        self._b.request(C.FS_UMOUNT, bytes([self._id]))


class Fs:
    """Filesystem access on the ESP32 — reached as ``esp.fs``.

        >>> vol = esp.fs.mount("littlefs")     # or "sd"/"sdmmc"
        >>> vol.write_file("/log.txt", b"boot ok")
    """

    def __init__(self, bridge):
        self._b = bridge
        bridge.require(C.Cap.FS, "filesystem")

    def mount(self, kind: str = "littlefs", *, cs: int = 5, sck: int = -1,
              miso: int = -1, mosi: int = -1, freq_mhz: int = 20) -> Volume:
        """Mount a filesystem and return its Volume.

        kind: "littlefs" (internal flash, auto-formats on first use),
        "sd" (card on SPI; give cs and optionally sck/miso/mosi),
        "sdmmc" (dedicated SDMMC slot — classic ESP32 / S3 boards).
        """
        fs_id = _IDS[kind]
        payload = bytes([fs_id])
        if kind == "sd":
            payload += struct.pack(">Bbbbb", cs, sck, miso, mosi, freq_mhz)
        r = self._b.request(C.FS_MOUNT, payload, timeout=10.0)  # SD init is slow
        total, used = struct.unpack(">II", r)
        return Volume(self._b, fs_id, total, used)
