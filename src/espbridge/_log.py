"""Library logger: "[espbridge]" prefix + uvicorn-style colored level prefix.

Mirrors uvicorn.logging.DefaultFormatter (same colors, same ``LEVEL:``
padding) without depending on uvicorn: colors auto-enable only when stderr
is a terminal that supports ANSI, and NO_COLOR is honored.
"""
from __future__ import annotations

import logging
import os
import sys

# uvicorn's palette: DEBUG cyan, INFO green, WARNING yellow, ERROR red,
# CRITICAL bright red.
_LEVEL_COLORS = {
    logging.DEBUG: 36,
    logging.INFO: 32,
    logging.WARNING: 33,
    logging.ERROR: 31,
    logging.CRITICAL: 91,
}


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not getattr(stream, "isatty", lambda: False)():
        return False
    if sys.platform == "win32":
        # Windows Terminal/VS Code have VT processing on already; flip it on
        # for the classic console. Failure means no ANSI support.
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-12)  # STD_ERROR_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(kernel32.SetConsoleMode(
            handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    return True


class _Formatter(logging.Formatter):
    def __init__(self, use_colors: bool):
        super().__init__("[espbridge] %(levelprefix)s %(message)s")
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        pad = " " * max(0, 8 - len(record.levelname))  # align like uvicorn
        if self.use_colors:
            color = _LEVEL_COLORS.get(record.levelno, 0)
            record.levelprefix = (
                f"\x1b[{color}m{record.levelname}\x1b[0m:{pad}")
        else:
            record.levelprefix = f"{record.levelname}:{pad}"
        return super().format(record)


log = logging.getLogger("espbridge")
if not log.handlers:
    if os.environ.get("ESPBRIDGE_DEBUG"):
        # Frame-level tracing (every request/response with command names)
        # without touching the script: ESPBRIDGE_DEBUG=1 python app.py
        log.setLevel(logging.DEBUG)
    elif log.level == logging.NOTSET:  # respect a level the app set first
        log.setLevel(logging.INFO)
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(_Formatter(_supports_color(sys.stderr)))
    log.addHandler(_handler)
    log.propagate = False
