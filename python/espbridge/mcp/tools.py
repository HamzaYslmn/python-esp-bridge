"""Peripheral tools for the espbridge MCP server — generated from the Bridge API.

Every public sub-API method becomes a tool named ``<subapi>_<method>`` carrying
that method's own signature and docstring, so the MCP surface *is* the Python
API: ``esp.i2c.read(addr, n)`` is the ``i2c_read`` tool, and a peripheral that
gains a method gains a tool with nothing to write here. Bytes travel as hex
strings in both directions.

Only the exceptions are spelled out: ``SKIP`` lists methods that can't be a tool
(they take a host callback, or hand back a live Python object), and the
composites at the bottom are the few tools that aren't a single method call.
"""
from __future__ import annotations

import importlib
import inspect
import sys
from dataclasses import fields, is_dataclass

from ..bridge import Bridge
from ..errors import BridgeTimeoutError, UnsupportedError
from .common import guarded, hex_str, info_dict, to_bytes

# Methods that cannot be a tool: they take a host callback, or return a live
# object (socket, file handle, port) that doesn't cross the wire. Anything here
# still worth exposing has a composite below.
SKIP = {
    "gpio": {"watch", "unwatch"},      # callback + host-side event stream
    "uart": {"init", "port"},          # hand back a UartPort -> uart_* below
    "wifi": {"on_state"},
    "nvs": {"set", "get"},             # polymorphic value -> typed variants below
    "espnow": {"on_receive", "on_send_result", "read"},
    "can": {"on_message"},
    "watch": {"on"},
    "camera": {"capture"},             # returns an image -> composite below
    "rmt": {"recv"},                   # blocks for a capture window
    "fs": {"mount"},                   # returns a Volume -> fs_* below
    "ota": {"flash"},                  # progress callback -> composite below
    "eth": {"begin"},                  # **kw preset overrides -> composite below
}
# Whole sub-APIs that are host-object APIs rather than request/response: every
# useful call hands back a socket or a live GATT session.
SKIP_SUBAPI = {"net", "ble"}

# Bridge's own methods, exposed as system_<name>.
SYSTEM = ("ping", "free_heap", "reset", "set_name", "deep_sleep", "light_sleep",
          "wake_cause", "cpu_freq", "power_mode", "link_power", "radio_off")

# Volume methods (esp.fs.mount(kind) -> Volume), exposed as fs_<name> with the
# volume kind as an extra argument. The rest of fs is composites below.
VOLUME = ("list", "stat", "remove", "rename", "mkdir", "df")

_JSON_TYPES = (int, float, str, bool, dict, list, type(None))
_JSON = int | float | str | bool | dict | list | None


def _jsonable(value):
    """Make a return value JSON-friendly, bytes as hex (what to_bytes reads back)."""
    if isinstance(value, (bytes, bytearray)):
        return hex_str(bytes(value))
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _json_return(annotation):
    """What the tool declares it returns, after _jsonable has had its way: bytes
    arrive as hex, and anything the schema generator can't pin down (dataclasses,
    unions of them) falls back to the permissive JSON union."""
    if annotation is bytes:
        return str
    if annotation in _JSON_TYPES or getattr(annotation, "__origin__", None) in (
            list, dict, tuple):
        return annotation
    return _JSON


def _tool(mcp, name: str, fn, target, extra=()):
    """Register ``fn`` as an MCP tool named ``name``, calling it on ``target()``.

    Signature and docstring come straight from the method; ``bytes`` parameters
    are declared as hex strings and decoded on the way in, and the result goes
    through :func:`_jsonable` on the way out. ``extra`` prepends parameters that
    ``target`` consumes rather than the method (the fs volume kind).
    """
    sig = inspect.signature(fn, eval_str=True,
                            globals=vars(sys.modules[fn.__module__]))
    # Drop self, *args/**kwargs (no JSON schema fits them) and callbacks (a host
    # function can't cross the wire) — what's left is the tool's parameters.
    given = [p for p in list(sig.parameters.values())[1:]
             if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
             and "Callable" not in str(p.annotation)]
    hexed = {p.name for p in given if "bytes" in str(p.annotation)}
    # All keyword-only: FastMCP calls tools by name, and it lets `extra` sit at
    # the end regardless of which method parameters are positional.
    params = [
        p.replace(kind=p.KEYWORD_ONLY, annotation=str,
                  default=hex_str(p.default) if isinstance(p.default, bytes)
                  else p.default)
        if p.name in hexed else p.replace(kind=p.KEYWORD_ONLY)
        for p in given
    ]
    ret = _json_return(sig.return_annotation)

    def impl(**kw):
        for k in hexed & kw.keys():
            kw[k] = to_bytes(kw[k])
        obj = target(*(kw.pop(p.name) for p in extra)) if extra else target()
        return _jsonable(fn(obj, **kw))

    annotations = {p.name: p.annotation for p in (*params, *extra)
                   if p.annotation is not inspect.Parameter.empty}
    if ret is not inspect.Parameter.empty:
        annotations["return"] = ret
    signature = sig.replace(parameters=[*params, *extra], return_annotation=ret)

    tool = guarded(impl)  # clean ToolErrors + the per-action feedback line
    for f in (impl, tool):  # FastMCP inspects either side of functools.wraps
        f.__name__ = name
        f.__doc__ = inspect.getdoc(fn)
        f.__signature__ = signature
        f.__annotations__ = annotations
    mcp.tool(tool)


def _methods(cls, skip=frozenset()):
    """Public methods of a sub-API class, with skipped ones and aliases removed.

    Both filters go by function, not name, so ``begin = init`` yields one tool —
    and skipping ``init`` also skips ``begin``.
    """
    seen = {fn for name, fn in vars(cls).items() if name in skip}
    out = []
    for name, fn in vars(cls).items():
        if name.startswith("_") or not callable(fn) or fn in seen:
            continue
        seen.add(fn)
        out.append((name, fn))
    return out


def register_all(mcp, mgr) -> None:
    """Register every peripheral tool on ``mcp``, bound to ``mgr``'s bridge."""
    for sub, (module, cls_name) in Bridge._SUBAPIS.items():
        if sub in SKIP_SUBAPI:
            continue
        cls = getattr(importlib.import_module(f"..{module}", __package__), cls_name)
        for name, fn in _methods(cls, SKIP.get(sub, frozenset())):
            _tool(mcp, f"{sub}_{name}", fn, lambda s=sub: getattr(mgr.bridge(), s))

    for name in SYSTEM:
        _tool(mcp, f"system_{name}", getattr(Bridge, name), mgr.bridge)

    from ..fs import Volume

    kind = inspect.Parameter("kind", inspect.Parameter.KEYWORD_ONLY,
                             default="littlefs", annotation=str)
    for name in VOLUME:
        _tool(mcp, f"fs_{name}", getattr(Volume, name), mgr.volume, extra=(kind,))

    _register_composites(mcp, mgr)


# ---- composites: the tools that are not one method call ----------------------

def _register_composites(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def system_info() -> dict:
        """Chip model, MAC, firmware version, flash size and capability list."""
        return info_dict(mgr.bridge())

    @mcp.tool
    @guarded
    def board_status() -> dict:
        """Whole-board snapshot — call this to see everything that's set up before
        acting. Returns: chip/firmware/heap; every active GPIO pin (configured or
        PWM-driven) with its mode, level and PWM freq/duty; and the I2C addresses
        that respond on each initialized bus.

        It reports the raw addresses that ACK, not what the parts are — an I2C
        address does not uniquely identify a device, so identify the actual
        hardware from your own wiring/datasheets. I2C buses appear here once
        you've called i2c_init."""
        esp = mgr.bridge()
        try:
            pins, note = _jsonable(esp.gpio.dump()), None
        except UnsupportedError as e:
            pins, note = [], str(e)  # older firmware without GPIO_DUMP
        i2c = [{"bus": bus, **cfg,
                "devices": [{"addr": a, "hex": f"0x{a:02x}"}
                            for a in esp.i2c.scan(bus)]}
               for bus, cfg in sorted(esp.i2c.buses.items())]
        return {
            **info_dict(esp),
            "free_heap": esp.free_heap()["free"],
            "pins": pins,
            "pins_note": note,
            "i2c": i2c,
            "i2c_note": None if i2c else "no I2C bus initialized yet — call "
            "i2c_init(sda=, scl=) to scan a bus for device addresses",
        }

    # -- uart: init hands back a live port, which esp.uart already keeps ------

    def _port(port: int):
        p = mgr.bridge().uart.port(port)
        if p is None:
            raise ValueError(f"UART{port} is not open — call uart_init first")
        return p

    @mcp.tool
    @guarded
    def uart_init(port: int = 1, tx: int = 17, rx: int = 16,
                  baud: int = 115_200) -> str:
        """Open a secondary UART (1 or 2). RX is buffered for uart_read."""
        mgr.bridge().uart.init(port=port, tx=tx, rx=rx, baud=baud)
        return f"UART{port} open (TX={tx}, RX={rx}, {baud} baud)"

    @mcp.tool
    @guarded
    def uart_write(port: int, data: str, text: bool = False) -> str:
        """Write to a UART. By default data is hex; set text=true to send data as
        a UTF-8 string instead."""
        payload = data.encode() if text else to_bytes(data)
        _port(port).write(payload)
        return f"wrote {len(payload)} bytes to UART{port}"

    @mcp.tool
    @guarded
    def uart_read(port: int, n: int | None = None, timeout: float = 1.0) -> dict:
        """Read buffered bytes from a UART (up to n; all if omitted). Returns both
        hex and a best-effort UTF-8 decode."""
        data = _port(port).read(n, timeout=timeout)
        return {"hex": hex_str(data), "text": data.decode("utf-8", "replace")}

    @mcp.tool
    @guarded
    def uart_close(port: int) -> str:
        """Close a secondary UART."""
        _port(port).close()
        return f"UART{port} closed"

    # -- nvs: one polymorphic set()/get() in Python, typed tools over MCP -----

    @mcp.tool
    @guarded
    def nvs_set_str(key: str, value: str) -> str:
        """Store a string under a key (max 15-byte key) in on-board NVS flash."""
        mgr.bridge().nvs.set(key, value)
        return f"set {key!r}"

    @mcp.tool
    @guarded
    def nvs_set_int(key: str, value: int) -> str:
        """Store a signed integer under a key in on-board NVS flash."""
        mgr.bridge().nvs.set(key, int(value))
        return f"set {key!r}"

    @mcp.tool
    @guarded
    def nvs_set_bytes(key: str, value: str) -> str:
        """Store raw hex bytes under a key in on-board NVS flash."""
        mgr.bridge().nvs.set(key, to_bytes(value))
        return f"set {key!r}"

    @mcp.tool
    @guarded
    def nvs_get_bytes(key: str) -> dict:
        """Read a key as raw bytes, returned as hex (null if the key is absent)."""
        v = mgr.bridge().nvs.get(key)
        return {"hex": None if v is None else hex_str(v)}

    # -- filesystem: mount caches the volume; text and bytes are separate -----

    @mcp.tool
    @guarded
    def fs_mount(kind: str = "littlefs", cs: int = 5, sck: int = -1, miso: int = -1,
                 mosi: int = -1, freq_mhz: int = 20) -> dict:
        """Mount a filesystem: "littlefs" (internal flash), "sd" (SPI card; give
        cs and optionally sck/miso/mosi) or "sdmmc". Returns total/used KiB."""
        vol = mgr.volume(kind, remount=True, cs=cs, sck=sck, miso=miso,
                         mosi=mosi, freq_mhz=freq_mhz)
        return {"kind": kind, "total_kb": vol.total_kb, "used_kb": vol.used_kb}

    @mcp.tool
    @guarded
    def fs_tree(path: str = "/", kind: str = "littlefs", max_depth: int = 6,
                max_entries: int = 1000) -> dict:
        """Recursively walk a filesystem subtree so you can see everything inside
        it at once: returns [{path, size, isdir}, ...] depth-first. Bounded by
        max_depth/max_entries (truncated=true if the cap was hit)."""
        vol, out = mgr.volume(kind), []

        def walk(p: str, depth: int) -> None:
            if depth > max_depth or len(out) >= max_entries:
                return
            for name, size, isdir in vol.list(p):
                full = f"{p.rstrip('/')}/{name}"
                out.append({"path": full, "size": size, "isdir": isdir})
                if isdir:
                    walk(full, depth + 1)

        walk(path, 0)
        return {"entries": out, "count": len(out),
                "truncated": len(out) >= max_entries}

    @mcp.tool
    @guarded
    def fs_read_text(path: str, kind: str = "littlefs") -> dict:
        """Read a file and decode it as UTF-8 text (best effort)."""
        data = mgr.volume(kind).read_file(path)
        return {"text": data.decode("utf-8", "replace"), "size": len(data)}

    @mcp.tool
    @guarded
    def fs_read_bytes(path: str, kind: str = "littlefs") -> dict:
        """Read a file and return its bytes as hex."""
        data = mgr.volume(kind).read_file(path)
        return {"hex": hex_str(data), "size": len(data)}

    @mcp.tool
    @guarded
    def fs_write_text(path: str, content: str, kind: str = "littlefs",
                      append: bool = False) -> str:
        """Write UTF-8 text to a file (absolute path; overwrites unless append)."""
        vol, data = mgr.volume(kind), content.encode()
        (vol.append_file if append else vol.write_file)(path, data)
        return f"wrote {len(data)} bytes to {path}"

    @mcp.tool
    @guarded
    def fs_write_bytes(path: str, content: str, kind: str = "littlefs",
                       append: bool = False) -> str:
        """Write hex bytes to a file (absolute path; overwrites unless append)."""
        vol, data = mgr.volume(kind), to_bytes(content)
        (vol.append_file if append else vol.write_file)(path, data)
        return f"wrote {len(data)} bytes to {path}"

    # -- the rest: variadic overrides, a progress callback, an image ----------

    @mcp.tool
    @guarded
    def eth_begin(preset_or_phy: str, addr: int | None = None, cs: int | None = None,
                  irq: int | None = None, rst: int | None = None,
                  freq_mhz: int | None = None) -> str:
        """Start Ethernet from a board preset (e.g. "wt32-eth01") or a PHY name
        (e.g. "w5500" with cs/irq/rst for an SPI module)."""
        kw = {k: v for k, v in dict(addr=addr, cs=cs, irq=irq, rst=rst,
                                    freq_mhz=freq_mhz).items() if v is not None}
        mgr.bridge().eth.begin(preset_or_phy, **kw)
        return f"Ethernet starting ({preset_or_phy})"

    @mcp.tool
    @guarded
    def ota_flash(file_path: str, reboot: bool = True) -> dict:
        """Flash a compiled firmware image (.bin file on the host) to the board's
        inactive app slot over the link. Returns bytes written."""
        written = mgr.bridge().ota.flash(file_path, reboot=reboot)
        return {"bytes_written": written, "rebooted": reboot}

    @mcp.tool
    @guarded
    def camera_capture():
        """Capture one JPEG frame and return it as an image."""
        jpeg = mgr.bridge().camera.capture()
        try:
            from fastmcp.utilities.types import Image

            return Image(data=jpeg, format="jpeg")
        except (ImportError, AttributeError):
            import base64

            return {"format": "jpeg", "size": len(jpeg),
                    "base64": base64.b64encode(jpeg).decode()}

    @mcp.tool
    @guarded
    def espnow_read(timeout: float = 1.0) -> dict | None:
        """Pop the oldest received ESP-NOW packet (null on timeout). Only works
        when no on-board callback is consuming packets."""
        try:
            mac, data, rssi = mgr.bridge().espnow.read(timeout=timeout)
        except BridgeTimeoutError:
            return None
        return {"mac": mac, "hex": hex_str(data), "rssi": rssi}
