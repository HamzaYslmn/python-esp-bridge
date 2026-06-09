"""MCP server smoke tests: build the server over the fake firmware and drive a
few tools end-to-end through an in-memory FastMCP client."""
import asyncio

import pytest

pytest.importorskip("fastmcp")
from fastmcp import Client  # noqa: E402

from espbridge.mcp import BridgeManager, build_server  # noqa: E402


def _manager(fw):
    return BridgeManager(transport=fw.transport, upgrade_baud=False,
                         reset_on_open=False)


def test_build_server_registers_tools(fw):
    mgr = _manager(fw)
    server = build_server(mgr)
    try:
        async def run():
            async with Client(server) as client:
                return {t.name for t in await client.list_tools()}

        names = asyncio.run(run())
    finally:
        mgr.disconnect()

    # a representative slice across the peripheral groups
    expected = {
        "bridge_connect", "bridge_status", "system_info", "system_ping",
        "gpio_mode", "gpio_write", "gpio_read", "adc_read", "pwm_servo",
        "i2c_scan", "i2c_read", "spi_transfer", "wifi_status", "nvs_set_str",
        "fs_list", "onewire_search", "espnow_begin", "can_status",
        "mcpwm_begin", "eth_status", "camera_capture", "ota_flash",
    }
    assert expected <= names


def test_tools_drive_the_bridge(fw):
    mgr = _manager(fw)
    server = build_server(mgr)
    fw.i2c_devices[0x3C] = b"\x01\x02\x03"

    async def run():
        async with Client(server) as client:
            info = await client.call_tool("system_info", {})
            assert info.data["chip"] == "ESP32"
            assert info.data["mac"] == "24:a1:60:12:34:56"

            await client.call_tool("gpio_mode", {"pin": 2, "mode": "output"})
            r = await client.call_tool("gpio_write", {"pin": 2, "value": 1})
            assert r.data["level"] == 1

            await client.call_tool("i2c_init", {})
            scan = await client.call_tool("i2c_scan", {})
            assert 0x3C in scan.data["addresses"]
            rd = await client.call_tool(
                "i2c_read", {"addr": 0x3C, "n": 3})
            assert rd.data["hex"] == "010203"

    try:
        asyncio.run(run())
    finally:
        mgr.disconnect()

    assert fw.gpio_levels[2] == 1
    assert fw.gpio_modes[2] == 1  # MODES["output"]


def _collect_feedback(server, fw, calls):
    """Run `calls` (list of (tool, params)) and return the feedback messages the
    client received as MCP log notifications."""
    msgs = []

    async def handler(m):
        data = getattr(m, "data", None)
        msgs.append(data.get("msg") if isinstance(data, dict) else str(data))

    async def run():
        async with Client(server, log_handler=handler) as client:
            for tool, params in calls:
                await client.call_tool(tool, params)

    asyncio.run(run())
    return msgs


def test_feedback_on_by_default(fw):
    from espbridge.mcp.common import FEEDBACK

    FEEDBACK.enabled = True
    mgr = _manager(fw)
    try:
        msgs = _collect_feedback(
            build_server(mgr), fw,
            [("gpio_mode", {"pin": 2, "mode": "output"}),
             ("gpio_write", {"pin": 2, "value": 1})])
    finally:
        mgr.disconnect()

    # the gpio_mode confirmation and the gpio_write read-back both surfaced
    assert any("GPIO2" in (m or "") and "output" in (m or "") for m in msgs)
    assert any("gpio_write" in (m or "") and "level" in (m or "") for m in msgs)


def test_feedback_can_be_disabled(fw):
    from espbridge.mcp.common import FEEDBACK

    FEEDBACK.enabled = True
    mgr = _manager(fw)
    server = build_server(mgr)
    msgs = []

    async def handler(m):
        data = getattr(m, "data", None)
        msgs.append(data.get("msg") if isinstance(data, dict) else str(data))

    async def run():
        async with Client(server, log_handler=handler) as client:
            await client.call_tool("bridge_feedback", {"enabled": False})
            msgs.clear()  # drop the toggle's own feedback
            await client.call_tool("gpio_mode", {"pin": 4, "mode": "output"})

    try:
        asyncio.run(run())
    finally:
        FEEDBACK.enabled = True  # restore the module-level default
        mgr.disconnect()

    assert msgs == []


def test_gpio_status_and_write_ok(fw):
    mgr = _manager(fw)
    server = build_server(mgr)

    async def run():
        async with Client(server) as c:
            await c.call_tool("gpio_mode", {"pin": 2, "mode": "output"})
            w = await c.call_tool("gpio_write", {"pin": 2, "value": 1})
            assert w.data["ok"] is True and w.data["level"] == 1
            s = await c.call_tool("gpio_status", {"pin": 2})
            assert s.data["mode"] == "output"
            assert s.data["level"] == 1
            assert s.data["pwm"] is False

    try:
        asyncio.run(run())
    finally:
        mgr.disconnect()


def test_board_status_sees_pins_and_i2c_devices(fw):
    fw.i2c_devices[0x3C] = b"\x00"          # pretend an SSD1306 OLED is wired up
    mgr = _manager(fw)
    server = build_server(mgr)

    async def run():
        async with Client(server) as c:
            await c.call_tool("gpio_mode", {"pin": 4, "mode": "output"})
            await c.call_tool("gpio_write", {"pin": 4, "value": 0})
            await c.call_tool("pwm_attach", {"pin": 5, "freq": 1000, "resolution_bits": 10})
            await c.call_tool("i2c_init", {"sda": 21, "scl": 22})
            return (await c.call_tool("board_status", {})).data

    try:
        status = asyncio.run(run())
    finally:
        mgr.disconnect()

    pins = {p["pin"]: p for p in status["pins"]}
    assert pins[4]["mode"] == "output" and pins[4]["level"] == 0
    assert pins[5]["pwm"] is True
    bus0 = next(b for b in status["i2c"] if b["bus"] == 0)
    addrs = {d["addr"] for d in bus0["devices"]}
    assert 0x3C in addrs                     # the device on the bus is surfaced
    assert all("guess" not in d for d in bus0["devices"])  # no presumptive naming


def test_fs_tree(fw):
    fw.fs_files[(0, "/a.txt")] = bytearray(b"hi")
    fw.fs_files[(0, "/b.txt")] = bytearray(b"yo")
    mgr = _manager(fw)
    server = build_server(mgr)

    async def run():
        async with Client(server) as c:
            await c.call_tool("fs_mount", {})
            t = await c.call_tool("fs_tree", {})
            return {e["path"] for e in t.data["entries"]}

    try:
        paths = asyncio.run(run())
    finally:
        mgr.disconnect()

    assert paths == {"/a.txt", "/b.txt"}


def test_feedback_on_failure(fw):
    """A failed action is reported too (warning), so every action is visible."""
    from fastmcp.exceptions import ToolError as ClientToolError

    from espbridge.mcp.common import FEEDBACK

    FEEDBACK.enabled = True
    mgr = _manager(fw)
    server = build_server(mgr)
    seen = []

    async def handler(m):
        data = getattr(m, "data", None)
        msg = data.get("msg") if isinstance(data, dict) else str(data)
        seen.append((getattr(m, "level", None), msg))

    async def run():
        async with Client(server, log_handler=handler) as client:
            with pytest.raises(ClientToolError):
                # GPIO_WRITE on a pin never set to output -> BAD_PIN error
                await client.call_tool("gpio_write", {"pin": 5, "value": 1})

    try:
        asyncio.run(run())
    finally:
        mgr.disconnect()

    assert any(lvl == "warning" and "gpio_write" in (m or "") and "FAILED" in (m or "")
               for lvl, m in seen)


def test_tool_error_is_clean(fw):
    """A peripheral failure (writing an un-configured pin) surfaces as a tool
    error rather than crashing the server."""
    from fastmcp.exceptions import ToolError as ClientToolError

    mgr = _manager(fw)
    server = build_server(mgr)

    async def run():
        async with Client(server) as client:
            with pytest.raises(ClientToolError):
                # GPIO_WRITE on a pin never put in output mode -> BAD_PIN
                await client.call_tool("gpio_write", {"pin": 5, "value": 1})

    try:
        asyncio.run(run())
    finally:
        mgr.disconnect()
