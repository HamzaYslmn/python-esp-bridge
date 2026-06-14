"""Command-line entry point: `espbridge`."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from ._log import log
from .bridge import Bridge, connect_all
from .errors import BridgeError
from .transports import find_ports


def _print_info(esp: Bridge) -> None:
    info = esp.info
    assert info is not None
    fw = ".".join(map(str, info.fw_version))
    print(f"name      : {info.name or '(unnamed — set with `espbridge set-name`)'}")
    print(f"chip      : {info.chip.name} rev {info.chip_rev}")
    print(f"mac       : {info.mac}")
    print(f"firmware  : v{fw} (protocol v{info.protocol})")
    print(f"flash     : {info.flash_mb} MB")
    print(f"gpio count: {info.gpio_count}")
    print(f"caps      : {info.caps!r}")
    print(f"ping      : {esp.ping() * 1000:.2f} ms")
    heap = esp.free_heap()
    print(f"free heap : {heap['free']} (min {heap['min_free']})")


def _force_utf8_output() -> None:
    """Print UTF-8 regardless of the console's locale (Windows defaults to cp1252,
    which mangles the em dashes / box characters in our output)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # not a reconfigurable TextIO (e.g. piped/redirected)
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    ap = argparse.ArgumentParser(prog="espbridge",
                                 description="python-esp-bridge host tool")
    ap.add_argument("--version", action="version", version=f"espbridge {__version__}")
    ap.add_argument("-p", "--port", help="serial port (default: auto-detect)")
    ap.add_argument("-n", "--name", help="select device by stored name")
    ap.add_argument("-b", "--ble", nargs="?", const=True, metavar="NAME_OR_MAC",
                    help="force Bluetooth (optionally to a named/MAC device); "
                         "the default already prefers Bluetooth")
    ap.add_argument("--usb", action="store_true",
                    help="USB serial only — disable Bluetooth")
    ap.add_argument("--password", help="Bluetooth link password "
                                       "(default: 'espbridge')")
    ap.add_argument("--no-baud-upgrade", action="store_true",
                    help="stay at 115200 instead of upgrading the link speed")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("ports", help="list ESP32-like serial ports")
    p_scan = sub.add_parser("scan", help="connect to every attached device and "
                                         "list port/name/chip/mac")
    p_scan.add_argument("--ble", action="store_true", dest="scan_ble",
                        help="scan for bridges advertising over Bluetooth")
    sub.add_parser("info", help="connect and print firmware/chip info (default; "
                                "shows every device when several are attached)")
    sub.add_parser("drivers", help="list registered device drivers "
                                   "(bundled + installed plugins)")
    p_name = sub.add_parser("set-name", help="store a device name on the ESP32 (NVS)")
    p_name.add_argument("new_name", help="name to assign (max 32 bytes)")
    p_flash = sub.add_parser("flash", help="write the bundled bridge firmware to a "
                                           "board over USB (needs the [flash] extra)")
    # A second -p on the subparser so `espbridge flash -p COM5` reads naturally;
    # SUPPRESS keeps it from clobbering the top-level -p when omitted.
    p_flash.add_argument("-p", "--port", dest="port", default=argparse.SUPPRESS,
                         help="serial port to flash (default: list ports and choose)")
    p_flash.add_argument("--baud", type=int, default=921600,
                         help="flash baud rate (default: 921600)")
    p_flash.add_argument("--erase", action="store_true",
                         help="erase the whole flash before writing (clears NVS / stored name)")
    p_flash.add_argument("--firmware", help="flash this .bin instead of the bundled image")
    p_flash.add_argument("--chip", default="esp32", help="target chip (default: esp32)")
    args = ap.parse_args(argv)

    kwargs = dict(upgrade_baud=not args.no_baud_upgrade)
    if args.usb:
        kwargs["ble"] = False
    elif args.ble:
        kwargs["ble"] = args.ble
    if args.password is not None:
        kwargs["password"] = args.password

    try:
        if args.cmd == "flash":
            from .flash import flash_firmware

            flash_firmware(args.port, baud=args.baud, erase=args.erase,
                           firmware=args.firmware, chip=args.chip)
            return 0

        if args.cmd == "scan" and args.scan_ble:
            from .transports.ble import find_ble_devices

            devs = find_ble_devices()
            if not devs:
                print("no bridges advertising over Bluetooth")
                return 1
            print(f"{'NAME':<16s} {'MAC':<18s} {'ADVERTISED':<32s} RSSI")
            for d in devs:
                print(f"{d.device_name or '-':<16s} {d.mac or '-':<18s} "
                      f"{d.name:<32s} {d.rssi} dBm")
            return 0

        if args.cmd == "drivers":
            from .drivers import driver_names, driver_source

            names = driver_names()
            if not names:
                print("no drivers registered")
                return 0
            width = max(len(n) for n in names) + len("esp.(...)")
            for n in names:
                print(f"{'esp.' + n + '(...)':<{width}}  {driver_source(n)}")
            print("\nWrite or install your own — see docs/DRIVERS.md")
            return 0

        if args.cmd == "ports":
            ports = find_ports()
            if not ports:
                print("no ESP32-like serial ports found")
                return 1
            for p in ports:
                print(f"{p.device}\t{p.usb_chip}\t{p.description}")
            return 0

        if args.cmd == "scan":
            ports = find_ports()
            if not ports:
                print("no ESP32-like serial ports found")
                return 1
            print(f"{'PORT':<12s} {'NAME':<16s} {'CHIP':<10s} {'MAC':<17s} FW")
            rc = 0
            for p in ports:
                try:
                    # skip the baud upgrade so each port is probed faster
                    with Bridge(p.device, upgrade_baud=False) as esp:
                        info = esp.info
                        fw = ".".join(map(str, info.fw_version))
                        print(f"{p.device:<12s} {info.name or '-':<16s} "
                              f"{info.chip.name:<10s} {info.mac:<17s} v{fw}")
                except BridgeError as e:
                    print(f"{p.device:<12s} error: {e}")
                    rc = 1
            return rc

        if args.cmd == "set-name":
            with Bridge(args.port, name=args.name, **kwargs) as esp:
                esp.set_name(args.new_name)
                print(f"{esp.info.mac} is now named {args.new_name!r}")
                print("(the Bluetooth advertised name updates on next reset)")
            return 0

        # default: info
        if args.port is None and args.name is None and not args.ble \
                and len(find_ports()) > 1:
            with connect_all(**kwargs) as boards:
                for i, esp in enumerate(boards):
                    if i:
                        print("-" * 40)
                    _print_info(esp)
            return 0
        with Bridge(args.port, name=args.name, **kwargs) as esp:
            _print_info(esp)
        return 0
    except BridgeError as e:
        log.error(str(e))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
