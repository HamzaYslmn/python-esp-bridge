"""FastMCP server that exposes a python-esp-bridge ESP32 as MCP tools.

Run it as a stdio MCP server (the default an MCP client launches):

    espbridge-mcp                       # auto-detect the USB serial port
    espbridge-mcp -p COM7               # a specific port
    espbridge-mcp -b                    # connect over Bluetooth (needs [ble])
    python -m espbridge.mcp --transport http --port-num 8000

Or embed it:

    from espbridge.mcp import build_server
    build_server(port="COM7").run()
"""
from __future__ import annotations

import argparse
import sys

from .. import __version__
from ..errors import BridgeError
from ..manager import BridgeManager as _CoreBridgeManager
from ..transports import find_ports
from .common import FEEDBACK, guarded, info_dict

try:
    from fastmcp import FastMCP
except ImportError as e:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "fastmcp is required for the MCP server: "
        "pip install 'python-esp-bridge[mcp]'"
    ) from e


INSTRUCTIONS = """\
Control an ESP32 running the python-esp-bridge firmware over USB or Bluetooth.

Tools are grouped by peripheral: system_*, gpio_*, adc_*/dac_*/touch_*, pwm_*,
i2c_*, spi_*, uart_*, wifi_*, nvs_*, fs_*, onewire_*, espnow_*, can_*, mcpwm_*,
eth_*, camera_*, ota_*. Connection is managed by bridge_connect / bridge_status
/ bridge_disconnect; if you call a peripheral tool before connecting, the server
auto-connects with the settings it was started with.

Conventions:
- Pins are integers (the chip's GPIO numbers). gpio_mode must be set to
  "output" before gpio_write, and to an input mode before gpio_read.
- Raw byte payloads are passed and returned as hex strings, e.g. "01ff" or
  "01 ff" (i2c/spi/uart/onewire/espnow/can data, nvs bytes, fs bytes).
- Capabilities vary by chip; check system_info().capabilities first. DAC, touch,
  CAN, camera, Ethernet etc. raise an error when the board/firmware lacks them.
- Every action emits live feedback (a log line for the operator + an MCP
  notification) so board activity is visible — successes as info, failures as
  warnings. It's on by default; toggle it with bridge_feedback(enabled=false/true).
"""


class BridgeManager(_CoreBridgeManager):
    """The core shared-bridge manager (one thread-safe, auto-reconnecting link)
    plus the stateful handles the MCP tools layer on top: mounted filesystem
    volumes, opened UART ports, and noted I2C buses (for board_status).

    Thread-safe: FastMCP runs each (synchronous) tool in a worker thread, and
    Bridge.request() is itself thread-safe, so concurrent tool calls share one
    connection without stepping on each other.
    """

    def __init__(self, **connect_kwargs):
        super().__init__(**connect_kwargs)
        self._volumes: dict = {}       # fs kind -> Volume
        self._uart_ports: dict = {}    # port number -> UartPort
        self._i2c_buses: dict = {}     # bus number -> {sda, scl, freq} (for board_status)

    def _close_locked(self) -> None:
        # Tear down the link, then drop the per-connection handles built on it.
        super()._close_locked()
        self._volumes.clear()
        self._uart_ports.clear()
        self._i2c_buses.clear()

    def volume(self, kind: str, *, remount: bool = False, **mount_kwargs):
        """Mount (and cache) a filesystem volume of the given kind.

        Lazy callers that just need *a* mount pass no kwargs and get the cached
        volume. An explicit `fs_mount` passes remount=True so the pins/options
        it was given are actually applied, instead of silently returning a
        volume mounted earlier with different (or default) pins.
        """
        with self._lock:
            vol = self._volumes.get(kind)
            if vol is None or remount:
                vol = self.bridge().fs.mount(kind, **mount_kwargs)
                self._volumes[kind] = vol
            return vol

    @property
    def i2c_buses(self) -> dict:
        with self._lock:
            return dict(self._i2c_buses)

    def note_i2c(self, bus: int, sda: int, scl: int, freq: int) -> None:
        with self._lock:
            self._i2c_buses[bus] = {"sda": sda, "scl": scl, "freq": freq}

    def forget_i2c(self, bus: int) -> None:
        with self._lock:
            self._i2c_buses.pop(bus, None)

    def uart_port(self, port: int):
        with self._lock:
            return self._uart_ports.get(port)

    def open_uart(self, *, port: int, tx: int, rx: int, baud: int):
        with self._lock:
            p = self.bridge().uart.init(port=port, tx=tx, rx=rx, baud=baud)
            self._uart_ports[port] = p
            return p

    def close_uart(self, port: int) -> None:
        with self._lock:
            p = self._uart_ports.pop(port, None)
            if p is not None:
                p.close()


def _register_connection(mcp: FastMCP, mgr: BridgeManager) -> None:
    @mcp.tool
    @guarded
    def bridge_list_ports() -> list[dict]:
        """List candidate ESP32 USB serial ports (device, USB chip, description)."""
        return [{"device": p.device, "usb_chip": p.usb_chip,
                 "description": p.description} for p in find_ports()]

    @mcp.tool
    @guarded
    def bridge_connect(name: str | None = None, port: str | None = None,
                       ble: bool = False, password: str | None = None) -> dict:
        """Connect to a board, replacing any current link, and return its info.

        name: the board's stored name, or its MAC — the way to pick one of
        several, over any link. port: serial port (e.g. "COM7" /
        "/dev/ttyUSB0"); omit to auto-detect. ble: Bluetooth only, instead of
        USB. password is the wireless link password (firmware default
        "espbridge").
        """
        esp = mgr.connect(name=name, port=port, ble=True if ble else None,
                          password=password)
        return {"connected": True, "info": info_dict(esp)}

    @mcp.tool
    @guarded
    def bridge_status() -> dict:
        """Report whether a board is connected and, if so, its info."""
        if not mgr.is_connected():
            return {"connected": False, "info": None}
        return {"connected": True, "info": info_dict(mgr.bridge())}

    @mcp.tool
    @guarded
    def bridge_disconnect() -> str:
        """Close the link to the board (a later tool call auto-reconnects)."""
        mgr.disconnect()
        return "disconnected"

    @mcp.tool
    @guarded
    def bridge_scan_ble(timeout: float = 5.0) -> list[dict]:
        """Scan for bridges advertising over Bluetooth (needs the [ble] extra)."""
        from ..transports.ble import find_ble_devices

        return [{"name": d.name or None, "mac": d.mac, "rssi": d.rssi}
                for d in find_ble_devices(timeout)]

    @mcp.tool
    @guarded
    def bridge_feedback(enabled: bool | None = None) -> dict:
        """Get or set live board-action feedback. Each tool then reports what it
        did, both to the server log and as an MCP notification you can see.
        Pass enabled=true/false to change it; omit to read the current state.
        On by default."""
        if enabled is not None:
            FEEDBACK.enabled = enabled
        return {"feedback_enabled": FEEDBACK.enabled}


def build_server(mgr: BridgeManager | None = None, **connect_kwargs) -> FastMCP:
    """Build a FastMCP server bound to a BridgeManager. Pass connection defaults
    (name=, port=, ble=, password=, upgrade_baud=) or a prebuilt manager."""
    if mgr is None:
        mgr = BridgeManager(**connect_kwargs)
    mcp = FastMCP(name="espbridge", instructions=INSTRUCTIONS)
    _register_connection(mcp, mgr)
    from .tools import register_all

    register_all(mcp, mgr)
    return mcp


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="espbridge-mcp",
        description="MCP server exposing a python-esp-bridge ESP32 as tools")
    ap.add_argument("--version", action="version", version=f"espbridge {__version__}")
    ap.add_argument("-n", "--name", metavar="NAME_OR_MAC",
                    help="select one board by its stored name, or its MAC")
    ap.add_argument("-p", "--port", help="serial port (default: auto-detect)")
    ap.add_argument("-b", "--ble", action="store_true",
                    help="connect over Bluetooth instead of USB")
    ap.add_argument("--password", help="wireless link password (default 'espbridge')")
    ap.add_argument("--no-baud-upgrade", action="store_true",
                    help="stay at 115200 instead of upgrading the link speed")
    ap.add_argument("--no-connect", action="store_true",
                    help="don't connect at startup; wait for the bridge_connect tool")
    ap.add_argument("--no-feedback", action="store_true",
                    help="don't emit per-action feedback (it is on by default)")
    ap.add_argument("--transport", default="stdio",
                    choices=["stdio", "http", "sse"],
                    help="MCP transport (default: stdio)")
    ap.add_argument("--host", default="127.0.0.1", help="bind host for http/sse")
    ap.add_argument("--port-num", type=int, default=8000,
                    help="TCP port for http/sse transports")
    args = ap.parse_args(argv)

    FEEDBACK.enabled = not args.no_feedback

    # BridgeManager drops the None entries; ble stays None unless -b was given.
    mgr = BridgeManager(
        name=args.name, port=args.port,
        ble=True if args.ble else None, password=args.password,
        upgrade_baud=not args.no_baud_upgrade,
    )
    if not args.no_connect:
        try:
            esp = mgr.connect()
            info = esp.info
            # Always write to stderr: stdout is the MCP protocol stream.
            print(f"[espbridge-mcp] connected: {info.chip.name} "
                  f"{info.mac} fw v{'.'.join(map(str, info.fw_version))}",
                  file=sys.stderr)
        except BridgeError as e:
            # Non-fatal: the manager will reconnect automatically on the
            # first tool call using the same startup settings.
            print(f"[espbridge-mcp] startup connect failed: {e} "
                  f"(will retry on the first tool call)", file=sys.stderr)

    server = build_server(mgr)
    try:
        if args.transport == "stdio":
            server.run()
        else:
            server.run(transport=args.transport, host=args.host, port=args.port_num)
    finally:
        mgr.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
