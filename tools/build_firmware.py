#!/usr/bin/env python3
"""Build the prebuilt firmware image that `espbridge flash` ships and writes.

Compiles examples/Bridge with the **Huge APP** partition scheme (3 MB app, no
OTA — the documented default for end users, biggest app, always reflash over
USB) and copies the merged image into the package at
``python/espbridge/firmware/`` so it gets bundled into the wheel.

    python tools/build_firmware.py            # build with arduino-cli on PATH
    python tools/build_firmware.py --fqbn esp32:esp32:esp32

Run in CI before `uv build`, or locally to refresh the bundled binary.

IMPORTANT — local cache: arduino-cli keys its build cache by FQBN, and the
partition scheme is part of the FQBN, so building Huge APP here would normally
evict a warm Minimal-SPIFFS dev cache (~minutes to rebuild). To avoid that this
script compiles into a *dedicated, isolated* build + cache path under
``tools/.fw-build/`` — your day-to-day min_spiffs cache is left untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKETCH = REPO_ROOT / "examples" / "Bridge"
DEST_DIR = REPO_ROOT / "python" / "espbridge" / "firmware"
DEST_NAME = "bridge.esp32.huge_app.bin"
# Isolated --build-path so the Huge APP build gets its own sketch build dir and
# never evicts the warm min_spiffs dev cache (the scheme is part of the FQBN, so
# a shared build dir would be thrown away on every scheme switch).
BUILD_DIR = REPO_ROOT / "tools" / ".fw-build" / "build"
OUT_DIR = REPO_ROOT / "tools" / ".fw-build" / "out"

# Classic-ESP32 image offsets, for the esptool merge fallback.
MERGE_OFFSETS = {"bootloader": 0x1000, "partitions": 0x8000,
                 "boot_app0": 0xE000, "app": 0x10000}


def _run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _merge_with_esptool(out_dir: Path, dest: Path, chip: str) -> None:
    """Fallback when the core didn't emit a *.merged.bin: stitch the parts."""
    import esptool

    boot = next(out_dir.glob("*.bootloader.bin"))
    parts = next(out_dir.glob("*.partitions.bin"))
    app = next(p for p in out_dir.glob("*.bin")
               if not p.name.endswith((".bootloader.bin", ".partitions.bin",
                                       ".merged.bin")))
    boot_app0 = next(REPO_ROOT.glob(
        f"**/packages/esp32/hardware/esp32/*/tools/partitions/boot_app0.bin"),
        None)
    argv = ["--chip", chip, "merge_bin", "-o", str(dest),
            hex(MERGE_OFFSETS["bootloader"]), str(boot),
            hex(MERGE_OFFSETS["partitions"]), str(parts)]
    if boot_app0:
        argv += [hex(MERGE_OFFSETS["boot_app0"]), str(boot_app0)]
    argv += [hex(MERGE_OFFSETS["app"]), str(app)]
    print("+ esptool", " ".join(argv))
    esptool.main(argv)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fqbn", default="esp32:esp32:esp32",
                    help="base FQBN; PartitionScheme=huge_app is appended")
    ap.add_argument("--arduino-cli", default="arduino-cli",
                    help="arduino-cli executable (default: on PATH)")
    ap.add_argument("--extra-flags", default="",
                    help="value for build.extra_flags (e.g. -DBRIDGE_ENABLE_ETH=1)")
    args = ap.parse_args(argv)

    if not SKETCH.is_dir():
        print(f"sketch not found: {SKETCH}", file=sys.stderr)
        return 1

    fqbn = f"{args.fqbn}:PartitionScheme=huge_app"
    chip = args.fqbn.split(":")[2] if args.fqbn.count(":") >= 2 else "esp32"
    for d in (BUILD_DIR, OUT_DIR):
        shutil.rmtree(d, ignore_errors=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [args.arduino_cli, "compile",
           "--fqbn", fqbn,
           "--library", str(REPO_ROOT),       # the repo root *is* the library
           "--build-path", str(BUILD_DIR),    # isolated: protects the dev cache
           "--output-dir", str(OUT_DIR)]
    if args.extra_flags:
        cmd += ["--build-property", f"build.extra_flags={args.extra_flags}"]
    cmd.append(str(SKETCH))
    _run(cmd)

    DEST_DIR.mkdir(parents=True, exist_ok=True)
    dest = DEST_DIR / DEST_NAME
    merged = next(OUT_DIR.glob("*.merged.bin"), None)
    if merged:
        shutil.copy2(merged, dest)
    else:
        print("no *.merged.bin from arduino-cli; merging with esptool")
        _merge_with_esptool(OUT_DIR, dest, chip)

    # A merged full-flash image starts with 0xFF padding; the 0xE9 ESP32 image
    # magic sits at the bootloader offset (0x1000 on classic ESP32/S2, 0x0 on
    # S3/C3). Accept either so the check works across chips.
    data = dest.read_bytes()
    if len(data) <= 0x1000 or (data[0] != 0xE9 and data[0x1000] != 0xE9):
        print(f"built image doesn't look like an ESP32 binary: {dest}",
              file=sys.stderr)
        return 1
    sha = hashlib.sha256(data).hexdigest()
    print(f"\nfirmware: {dest}")
    print(f"size    : {len(data) / 1024:.0f} KB")
    print(f"sha256  : {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
