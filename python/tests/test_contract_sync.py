"""Guard: src/espbridge/commands.h and espbridge.constants must stay in sync.

Both files carry a "MUST stay in sync" banner; this test enforces it. It parses
the firmware header and asserts every command id, module id, status code,
capability bit, and protocol scalar has the matching value in the Python mirror —
catching the classic drift where a constant is added or changed in one file but
not the other (which silently corrupts the wire protocol).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from espbridge import constants as C

HEADER = Path(__file__).resolve().parents[2] / "src" / "espbridge" / "commands.h"

# Firmware-only constants with no Python mirror (firmware version is intentionally
# decoupled from the package version, and these scalars aren't used host-side).
_FW_ONLY = {"FW_VERSION_MAJOR", "FW_VERSION_MINOR", "FW_VERSION_PATCH"}

# Plain protocol scalars that must match exactly.
_SCALARS = [
    "PROTOCOL_VERSION", "MAX_PAYLOAD", "NET_CHUNK", "UART_CHUNK", "OTA_CHUNK",
    "BRIDGE_NAME_MAX", "RMT_MAX_RX_SYMS", "ESPNOW_MAX_DATA",
    "FLAG_EVENT", "FLAG_ERROR", "FLAG_MORE",
    "GATT_PROP_READ", "GATT_PROP_WRITE", "GATT_PROP_NOTIFY", "GATT_PROP_WRITE_NR",
]


def _parse_header(text: str):
    ints: dict[str, int] = {}
    cmd_names: set[str] = set()

    def resolve(expr: str):
        expr = expr.strip()
        if re.fullmatch(r"0x[0-9A-Fa-f]+|\d+", expr):
            return int(expr, 0)
        m = re.fullmatch(r"CMD\((\w+),\s*(0x[0-9A-Fa-f]+|\d+)\)", expr)
        if m and m.group(1) in ints:
            return (ints[m.group(1)] << 8) | int(m.group(2), 0)
        m = re.fullmatch(r"\(1U?L?\s*<<\s*(\d+)\)", expr)
        if m:
            return 1 << int(m.group(1))
        m = re.fullmatch(r"\((\w+)\s*([-+])\s*(\d+)\)", expr)
        if m and m.group(1) in ints:
            base = ints[m.group(1)]
            return base - int(m.group(3)) if m.group(2) == "-" else base + int(m.group(3))
        return None

    defines = re.findall(r"^#define\s+(\w+)\s+(.+?)\s*(?://.*)?$", text, re.MULTILINE)
    for _ in range(3):  # passes let forward refs (CMD->MOD_*, OTA_CHUNK->MAX_PAYLOAD) resolve
        for name, raw in defines:
            if name in ints:
                continue
            val = resolve(raw)
            if val is not None:
                ints[name] = val
                if raw.strip().startswith("CMD("):
                    cmd_names.add(name)

    status: dict[str, int] = {}
    block = re.search(r"enum\s+Status\s*:\s*uint8_t\s*\{(.+?)\}", text, re.DOTALL)
    if block:
        for name, val in re.findall(r"ST_(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)", block.group(1)):
            status[name] = int(val, 0)

    return ints, cmd_names, status


_INTS, _CMD_NAMES, _STATUS = _parse_header(HEADER.read_text(encoding="utf-8"))


def test_header_was_parsed():
    """Sanity-check the parser actually found the contract, so the asserts below
    aren't passing vacuously."""
    assert HEADER.exists(), HEADER
    assert _INTS.get("MAX_PAYLOAD") == 2048
    assert len(_STATUS) >= 15
    assert len(_CMD_NAMES) > 100, "expected the full command surface"


@pytest.mark.parametrize("name", _SCALARS)
def test_scalar_matches(name):
    assert getattr(C, name) == _INTS[name], f"{name}: header={_INTS[name]} python={getattr(C, name, None)}"


@pytest.mark.parametrize("name", sorted(n for n in _INTS if n.startswith("MOD_")))
def test_module_id_matches(name):
    assert getattr(C, name) == _INTS[name]


@pytest.mark.parametrize("name", sorted(_CMD_NAMES))
def test_command_id_matches(name):
    py = getattr(C, name, None)
    assert py is not None, f"{name} is in commands.h but missing from constants.py"
    assert int(py) == _INTS[name], f"{name}: header=0x{_INTS[name]:04X} python=0x{int(py):04X}"


@pytest.mark.parametrize("name", sorted(_STATUS))
def test_status_code_matches(name):
    assert C.Status[name].value == _STATUS[name]


@pytest.mark.parametrize("name", sorted(n for n in _INTS if n.startswith("CAP_")))
def test_capability_bit_matches(name):
    assert C.Cap[name[4:]].value == _INTS[name]
