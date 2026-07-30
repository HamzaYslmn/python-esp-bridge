"""Flash the bundled bridge firmware to an ESP32 over USB — no Arduino IDE.

A prebuilt "Huge APP" (3 MB app, no OTA) merged image of ``examples/Bridge``
ships inside the package; this writes it to the board over the USB serial
cable with esptool, so users never have to open the Arduino IDE to get the
bridge firmware running.

    espbridge flash               # list ports, pick one, flash
    espbridge flash -p COM5       # flash a specific port
    espbridge flash --erase       # wipe the whole flash first (clears NVS)

Needs the [flash] extra (pulls in esptool):

    uvx --from "python-esp-bridge[flash]" espbridge flash
    pip install "python-esp-bridge[flash]"

This is the one-time initial flash over the cable. Once the firmware is
running, later updates can also go over the air with ``espbridge.ota`` — but
note the Huge APP image bundled here has no OTA partition, so cable-free
updates need the Minimal SPIFFS scheme instead (see docs/FIRMWARE.md).
"""
from __future__ import annotations

import sys
from importlib import resources
from pathlib import Path

from ._log import log
from .errors import BridgeError
from .transports import PortInfo, find_ports

# The bundled image is a classic-ESP32 Huge APP build: one merged image that
# already carries the bootloader + partition table + app, written at 0x0.
FIRMWARE_CHIP = "esp32"
FIRMWARE_OFFSET = "0x0"
DEFAULT_BAUD = 921600  # Arduino's default upload speed; safe on every adapter
_FIRMWARE_PKG = "espbridge.firmware"


def firmware_path() -> Path:
    """Filesystem path to the bundled merged firmware image.

    Raises BridgeError when the package was built without the binary — e.g. a
    source checkout that hasn't run ``tools/build_firmware.py`` yet. Release
    wheels from PyPI always include it.
    """
    try:
        root = resources.files(_FIRMWARE_PKG)
        for entry in root.iterdir():
            if entry.name.endswith(".bin") and entry.is_file():
                # Wheels install unzipped, so the traversable is a real path.
                return Path(str(entry))
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    raise BridgeError(
        "no bundled firmware image found — this build of python-esp-bridge "
        "shipped without the prebuilt binary.\n"
        "Build it with `python tools/build_firmware.py`, install a release "
        "wheel (`pip install python-esp-bridge`), or pass --firmware <path>."
    )


def _all_serial_ports() -> list[PortInfo]:
    """Every serial port, ESP32-like or not — a blank board may enumerate on an
    adapter we don't recognise, so don't hide ports just because the VID/PID
    isn't in the known list."""
    from serial.tools import list_ports

    return [PortInfo(p.device, None, p.description or "")
            for p in list_ports.comports() if p.device]


def _choose_port(ports: list[PortInfo]) -> str:
    """Print a numbered menu and read the operator's choice (Enter picks #1)."""
    print("Available serial ports:")
    for i, p in enumerate(ports, 1):
        chip = f"  [{p.usb_chip}]" if p.usb_chip else ""
        desc = f"  {p.description}" if p.description else ""
        print(f"  {i}) {p.device}{chip}{desc}")
    prompt = f"Select a port [1-{len(ports)}, Enter = 1, q = cancel]: "
    while True:
        try:
            raw = input(prompt).strip()
        except EOFError:
            raise BridgeError("no port selected (no interactive console; "
                              "pass -p PORT explicitly)")
        if raw.lower() in ("q", "quit"):
            raise BridgeError("cancelled")
        if raw == "":  # bare Enter → the first (most likely) port
            return ports[0].device
        if raw.isdigit() and 1 <= int(raw) <= len(ports):
            return ports[int(raw) - 1].device
        for p in ports:  # also accept typing the port name directly
            if raw.lower() == p.device.lower():
                return p.device
        print("  invalid selection — enter a number from the list")


def select_port(port: str | None = None) -> str:
    """Resolve a port: honour an explicit one, else show the menu (or, with no
    interactive console, auto-pick a lone port)."""
    if port:
        return port
    ports = find_ports() or _all_serial_ports()
    if not ports:
        raise BridgeError("no serial ports found — plug the ESP32 in over USB, "
                          "or pass -p PORT")
    interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
    if not interactive:  # piped/CI: can't prompt, so a lone port or bust
        if len(ports) == 1:
            log.info(f"using the only serial port found: {ports[0].device}")
            return ports[0].device
        names = ", ".join(p.device for p in ports)
        raise BridgeError(f"several serial ports ({names}); pass -p PORT "
                          "(no interactive console to choose from)")
    return _choose_port(ports)


def flash_firmware(port: str | None = None, *,
                   baud: int = DEFAULT_BAUD,
                   erase: bool = False,
                   firmware: str | Path | None = None,
                   chip: str = FIRMWARE_CHIP) -> None:
    """Write the (bundled, or given) firmware image to a board over USB serial.

    Drives esptool exactly the way the Arduino toolchain does for the initial
    cable flash. Raises BridgeError on any failure.
    """
    try:
        import esptool
    except ModuleNotFoundError:
        raise BridgeError(
            "esptool is required to flash. Install the [flash] extra:\n"
            '  pip install "python-esp-bridge[flash]"\n'
            '  uvx --from "python-esp-bridge[flash]" espbridge flash'
        )

    fw = Path(firmware) if firmware else firmware_path()
    if not fw.is_file():
        raise BridgeError(f"firmware image not found: {fw}")

    port = select_port(port)

    # All-dashed spellings throughout: esptool 5 renamed every underscore form
    # and the [flash] extra requires >=5, so there is no older syntax to serve.
    argv = ["--chip", chip, "--port", port, "--baud", str(baud),
            "--before", "default-reset", "--after", "hard-reset", "write-flash"]
    if erase:
        argv.append("--erase-all")
    argv += ["--flash-size", "keep", FIRMWARE_OFFSET, str(fw)]

    size_kb = fw.stat().st_size / 1024
    log.info(f"flashing {fw.name} ({size_kb:.0f} KB) to {port} at {baud} baud")
    try:
        esptool.main(argv)
    except SystemExit as e:  # esptool calls sys.exit() on argument/connect errors
        if e.code:
            raise BridgeError(f"esptool exited with status {e.code}")
    except KeyboardInterrupt:
        raise
    except Exception as e:  # FatalError and friends — surface a clean message
        raise BridgeError(f"flash failed: {e}")
    log.info("done — the board will reboot into the bridge firmware "
             "(connect with `espbridge info`)")


def main(argv: list[str] | None = None) -> int:
    """Entry point for the standalone `flash` command, so the one-off is just

        uvx --from "python-esp-bridge[flash]" flash

    `espbridge flash` runs the same thing through the main CLI.
    """
    import argparse

    from . import __version__
    from .cli import _force_utf8_output

    _force_utf8_output()
    ap = argparse.ArgumentParser(
        prog="flash",
        description="Flash the bundled bridge firmware to an ESP32 over USB.")
    ap.add_argument("--version", action="version", version=f"espbridge {__version__}")
    ap.add_argument("-p", "--port", help="serial port to flash (default: list ports and choose)")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                    help=f"flash baud rate (default: {DEFAULT_BAUD})")
    ap.add_argument("--erase", action="store_true",
                    help="erase the whole flash before writing (clears NVS / stored name)")
    ap.add_argument("--firmware", help="flash this .bin instead of the bundled image")
    ap.add_argument("--chip", default=FIRMWARE_CHIP, help=f"target chip (default: {FIRMWARE_CHIP})")
    args = ap.parse_args(argv)
    try:
        flash_firmware(args.port, baud=args.baud, erase=args.erase,
                       firmware=args.firmware, chip=args.chip)
    except BridgeError as e:
        log.error(str(e))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
