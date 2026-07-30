"""Run the ESP32 bridge as an MCP server so an AI agent can drive it.

Install the extra first:  pip install "python-esp-bridge[mcp]"

Then run this file (or just the installed `espbridge-mcp` console script):

    python examples/system/mcp_server.py            # stdio, auto-detect port
    python examples/system/mcp_server.py -p COM7    # a specific port

Point any MCP client at it. For Claude Code:

    claude mcp add espbridge -- espbridge-mcp -p COM7

This example also shows how to register your own extra tool on top of the 100+
built-in peripheral tools before serving. See docs/MCP.md for the full reference.
"""
from espbridge.mcp import BridgeManager, build_server

# Construct the manager explicitly so the custom tool below can reach the board.
# Pass "relays" (a name or MAC) / port="COM7" / ble=True here to pin one board;
# with no arguments it auto-detects the USB serial port.
mgr = BridgeManager()
server = build_server(mgr)


@server.tool
def blink(pin: int, times: int = 3, period_s: float = 0.2) -> str:
    """Blink an LED on `pin` a few times (a convenience on top of gpio_*)."""
    import time

    esp = mgr.bridge()
    esp.gpio.mode(pin, "output")
    for _ in range(times):
        esp.gpio.write(pin, 1)
        time.sleep(period_s)
        esp.gpio.write(pin, 0)
        time.sleep(period_s)
    return f"blinked GPIO{pin} {times} times"


if __name__ == "__main__":
    # Default stdio transport (what an MCP client launches). For HTTP instead:
    #   server.run(transport="http", host="127.0.0.1", port=8000)
    try:
        server.run()
    finally:
        mgr.disconnect()
