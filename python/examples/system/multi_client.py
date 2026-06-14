"""Many clients, one board: a single owner process holds the link; every client
talks to the owner, not the board.

A board's link can't be opened twice -- and not just across threads (see
basics/shared_connection.py) but across PROCESSES and machines too. On top of
that, most OSes allow only ONE BLE connection to a given device, so two programs
can't each open the same board over Bluetooth even if they try. The fix is one
*owner* that holds the single link and serves it; any number of clients connect
to the owner.

Here the owner is the built-in espbridge MCP server over HTTP and the clients are
plain MCP clients -- but the shape is general: one process owns the board, others
reach it over a local (or network) endpoint. Needs the extra:

    pip install "python-esp-bridge[mcp]"

    uv run system/multi_client.py            # owner auto-detects the USB port
    uv run system/multi_client.py -b         # owner connects over Bluetooth
    uv run system/multi_client.py -p COM7    # owner on a specific port

Three clients then hit the one owner at once; their calls share the single board
link and stay correctly correlated.
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time

from fastmcp import Client

HOST, PORT = "127.0.0.1", 8191
URL = f"http://{HOST}:{PORT}/mcp"


def _port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


async def client(n: int) -> str:
    # A separate client -- this could just as well be another process, or another
    # machine on the network. It never opens the board; it asks the owner.
    async with Client(URL) as c:
        info = (await c.call_tool("system_info", {})).data
        for _ in range(10):
            await c.call_tool("system_ping", {})   # round-trips through the one link
        return f"[client {n}] {info['chip']} {info['mac']} -- 10 pings OK"


async def run_clients(n: int) -> list[str]:
    return await asyncio.gather(*(client(i) for i in range(n)))


def main() -> int:
    # Start the owner: the espbridge MCP server over HTTP. It opens the ONE board
    # link; any extra CLI args (-p COM7 / -b / -m MAC) pick the device.
    owner = subprocess.Popen(
        [sys.executable, "-m", "espbridge.mcp", "--transport", "http",
         "--host", HOST, "--port-num", str(PORT), *sys.argv[1:]],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(60):
            if owner.poll() is not None:
                print(owner.stdout.read() if owner.stdout else "owner exited early")
                return 1
            if _port_open():
                break
            time.sleep(0.5)
        else:
            print("owner did not start listening")
            return 1
        print(f"owner up at {URL} (holds the single board link)\n")

        for line in asyncio.run(run_clients(3)):
            print(line)
        print("\nThree clients shared ONE board link through the owner. Add as many\n"
              "as you like -- separate processes, or other machines pointing at this\n"
              "host:port. The board is opened exactly once, by the owner.")
        return 0
    finally:
        owner.terminate()
        try:
            owner.wait(timeout=5)
        except Exception:
            owner.kill()


if __name__ == "__main__":
    raise SystemExit(main())
