"""Command-line entry point: `espbridge`."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from . import constants
from ._log import log
from .bridge import Bridge
from .errors import BridgeError
from .transports import find_ports


def _print_info(esp: Bridge) -> None:
    info = esp.info
    assert info is not None
    fw = ".".join(map(str, info.fw_version))
    print(f"name      : {info.name or '(unnamed — set with `espbridge set-name`)'}")
    print(f"mac       : {info.mac}")
    print(f"chip      : {info.chip.name} rev {info.chip_rev}")
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


def _connect_kwargs(args) -> dict:
    """Bridge() settings from the global options."""
    kwargs = {"upgrade_baud": not args.no_baud_upgrade, "name": args.name,
              "port": args.port}
    if args.host:
        kwargs.update(host=args.host, tcp_port=args.tcp_port)
    elif args.wifi:
        kwargs.update(wifi=True, tcp_port=args.tcp_port)
    elif args.usb:
        kwargs["ble"] = False
    elif args.ble:
        kwargs["ble"] = True
    if args.password is not None:
        kwargs["password"] = args.password
    return kwargs


# ---- subcommands (one per `sub.add_parser(...).set_defaults(fn=...)`) ---------


def cmd_ports(args) -> int:
    ports = find_ports()
    if not ports:
        print("no ESP32-like serial ports found")
        return 1
    for p in ports:
        print(f"{p.device}\t{p.usb_chip}\t{p.description}")
    return 0


def cmd_drivers(args) -> int:
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


def cmd_scan(args) -> int:
    if args.scan_ble:
        from .transports.ble import find_ble_devices

        devs = find_ble_devices()
        if not devs:
            print("no bridges advertising over Bluetooth")
            return 1
        # One column, because only one identity fits an advertisement: a named
        # board broadcasts its name, an unnamed one its MAC. Either way this is
        # the string to pass to Bridge().
        print(f"{'DEVICE':<20s} RSSI")
        for d in devs:
            print(f"{d.ident or '(unknown)':<20s} {d.rssi} dBm")
        return 0

    if args.scan_wifi:
        from .transports.tcp import find_wifi_devices

        devs = find_wifi_devices(port=args.tcp_port)
        if not devs:
            print("no bridges answered the Wi-Fi discovery broadcast "
                  "(dial-home boards do not answer — they connect to you; "
                  "use Bridge.all(wifi=True))")
            return 1
        print(f"{'NAME':<14s} {'MAC':<18s} ADDRESS")
        for d in devs:
            print(f"{d.name or '-':<14s} {d.mac:<18s} {d.host}:{d.port}")
        return 0

    ports = find_ports()
    if not ports:
        print("no ESP32-like serial ports found")
        return 1
    print(f"{'PORT':<12s} {'NAME':<14s} {'MAC':<18s} {'CHIP':<10s} FW")
    rc = 0
    for p in ports:
        try:
            # skip the baud upgrade so each port is probed faster
            with Bridge(port=p.device, upgrade_baud=False) as esp:
                info = esp.info
                fw = ".".join(map(str, info.fw_version))
                print(f"{p.device:<12s} {info.name or '-':<14s} "
                      f"{info.mac:<18s} {info.chip.name:<10s} v{fw}")
        except BridgeError as e:
            print(f"{p.device:<12s} error: {e}")
            rc = 1
    return rc


def cmd_flash(args) -> int:
    from .flash import flash_firmware

    flash_firmware(args.port, baud=args.baud, erase=args.erase,
                   firmware=args.firmware, chip=args.chip)
    return 0


def cmd_set_name(args) -> int:
    with Bridge(**_connect_kwargs(args)) as esp:
        esp.set_name(args.new_name)
        print(f"{esp.info.mac} is now named {args.new_name!r}")
        print("(the Bluetooth advertised name updates on next reset)")
    return 0


def cmd_info(args) -> int:
    """Print firmware/chip info — every board, or just the one you named."""
    with Bridge.all(**_connect_kwargs(args)) as found:
        for i, esp in enumerate(found):
            if i:
                print("-" * 40)
            _print_info(esp)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="espbridge",
                                 description="python-esp-bridge host tool")
    ap.add_argument("--version", action="version", version=f"espbridge {__version__}")
    ap.add_argument("-n", "--name", metavar="NAME_OR_MAC",
                    help="select one board by its stored name, or its MAC "
                         "(the way to pick one of several)")
    ap.add_argument("-p", "--port", help="serial port (default: auto-detect)")
    ap.add_argument("-b", "--ble", action="store_true",
                    help="Bluetooth only (default: USB, then Bluetooth, then Wi-Fi)")
    ap.add_argument("--usb", action="store_true",
                    help="USB serial only — no radio")
    ap.add_argument("--host", metavar="ADDR",
                    help="connect over Wi-Fi to a board at this address")
    ap.add_argument("--tcp-port", type=int, default=constants.BRIDGE_LINK_PORT,
                    help=f"Wi-Fi link port (default: {constants.BRIDGE_LINK_PORT})")
    ap.add_argument("-w", "--wifi", action="store_true",
                    help="find boards over Wi-Fi with a discovery broadcast")
    ap.add_argument("--password", help="wireless link password "
                                       "(default: 'espbridge')")
    ap.add_argument("--no-baud-upgrade", action="store_true",
                    help="stay at 115200 instead of upgrading the link speed")
    # info is the default command; every subparser names its own handler, so
    # main() dispatches instead of matching on args.cmd.
    ap.set_defaults(fn=cmd_info)
    sub = ap.add_subparsers()
    sub.add_parser("ports", help="list ESP32-like serial ports").set_defaults(
        fn=cmd_ports)
    sub.add_parser("info", help="connect and print firmware/chip info (default; "
                                "shows every board when several are attached)"
                   ).set_defaults(fn=cmd_info)
    sub.add_parser("drivers", help="list registered device drivers "
                                   "(bundled + installed plugins)").set_defaults(
        fn=cmd_drivers)
    p_name = sub.add_parser("set-name", help="store a device name on the board, "
                                             f"max {constants.BRIDGE_NAME_MAX} "
                                             "chars")
    p_name.set_defaults(fn=cmd_set_name)
    p_name.add_argument("new_name", help="name to assign; "
                                         "Bridge('<name>') then finds this board")
    p_scan = sub.add_parser("scan", help="connect to every attached device and "
                                         "list port/name/mac/chip")
    p_scan.set_defaults(fn=cmd_scan)
    p_scan.add_argument("--ble", action="store_true", dest="scan_ble",
                        help="scan for bridges advertising over Bluetooth")
    p_scan.add_argument("--wifi", action="store_true", dest="scan_wifi",
                        help="broadcast a discovery probe for bridges on the LAN")
    p_flash = sub.add_parser("flash", help="write the bundled bridge firmware to a "
                                           "board over USB (needs the [flash] extra)")
    p_flash.set_defaults(fn=cmd_flash)
    # A second -p on the subparser so `espbridge flash -p COM5` reads naturally;
    # SUPPRESS keeps it from clobbering the top-level -p when omitted.
    p_flash.add_argument("-p", "--port", dest="port", default=argparse.SUPPRESS,
                         help="serial port to flash (default: list ports and choose)")
    p_flash.add_argument("--baud", type=int, default=921600,
                         help="flash baud rate (default: 921600)")
    p_flash.add_argument("--erase", action="store_true",
                         help="erase the whole flash before writing (clears NVS)")
    p_flash.add_argument("--firmware", help="flash this .bin instead of the bundled image")
    p_flash.add_argument("--chip", default="esp32", help="target chip (default: esp32)")
    return ap


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    args = _build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except BridgeError as e:
        log.error(str(e))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
