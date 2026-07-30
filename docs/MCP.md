# MCP server — drive the bridge from an AI agent

`espbridge.mcp` is a [Model Context Protocol](https://modelcontextprotocol.io)
server that exposes a connected ESP32 to an LLM as a set of tools. The agent can
then control every peripheral the bridge supports — read an ADC, scan I2C,
toggle a relay, join Wi-Fi, snap a camera frame — in plain language.

It wraps the same `espbridge.Bridge` API the rest of the library uses, so the
firmware does not change: flash it once (see [FIRMWARE.md](FIRMWARE.md)), then
run the MCP server on the host.

## Install (once)

Get the `espbridge-mcp` command onto your PATH:

```bash
uv tool install "python-esp-bridge[mcp]"     # or: pip install "python-esp-bridge[mcp]"
```

Then plug your ESP32 into USB. Every assistant below launches that same
`espbridge-mcp` command and **auto-detects the board** — it tries Bluetooth
first, then falls back to USB serial — so there is nothing else to configure. Ask things like *"read GPIO 34"* or *"scan the I2C bus"*.

> Working from a local clone of this repo (unreleased changes)? Install the
> source instead: `uv pip install -e "python/[mcp]"`.

## Set it up in your assistant

### Claude Code

This repo ships [`.mcp.json`](../.mcp.json) — open Claude Code in the repo and
choose **Yes** when it offers to enable the `espbridge` server. Verify with
`claude mcp list`.

### Gemini CLI

This repo ships [`.gemini/settings.json`](../.gemini/settings.json) — open Gemini
CLI in the repo and run `/mcp` to confirm. To add it globally instead:
`gemini mcp add espbridge espbridge-mcp`.

### Codex CLI

One command:

```bash
codex mcp add espbridge -- espbridge-mcp
```

List with `codex mcp list`.

### Antigravity

In the Agent panel: **⋯** → **MCP Servers** → **Manage MCP Servers** → **View raw
config**, paste this, save, then **Refresh**:

```json
{ "mcpServers": { "espbridge": { "command": "espbridge-mcp", "args": [] } } }
```

(That edits `~/.gemini/config/mcp_config.json` — on Windows
`C:\Users\<you>\.gemini\config\mcp_config.json` — which you can also edit by hand.)

### Ollama

Ollama is a model runtime, not an MCP client, so use a small Ollama-native MCP
client. Simplest, no config file:

```bash
ollama pull llama3.2                                        # a tool-capable model
uvx ollmcp --mcp-server-command "espbridge-mcp" --model llama3.2
```

Tool use needs a tool-capable model (e.g. `llama3.2`, `qwen2.5`).

### Cursor / Windsurf / Claude Desktop / other clients

Add this to the client's MCP config (the key is `mcpServers`):

```json
{ "mcpServers": { "espbridge": { "command": "espbridge-mcp", "args": [] } } }
```

## Pick a board / Bluetooth / HTTP

The default auto-detects the USB port. To pin a board or change transport, add
args — to the `args` array in a JSON config, or after `--` in the
`codex mcp add` / `gemini mcp add` commands:

- `-p COM7` — a fixed serial port
- `-n relays` — select a board by its stored name
- `-b` — Bluetooth instead of USB (built in; no extra needed)
- `--transport http --host 0.0.0.0 --port-num 8000` — serve over HTTP for remote / multi-client use

`espbridge-mcp --help` lists every flag. The server connects at startup and
auto-reconnects on the first tool call if the link drops.

## Tools

One tool per method of the Python API — `esp.i2c.read(addr, n)` is the `i2c_read`
tool, with that method's own arguments and docstring, so the two never drift.
The agent reads the per-tool descriptions; the groups are:

| prefix | what |
|--------|------|
| `bridge_*` | connect / disconnect / status, list ports, BLE scan, feedback toggle |
| `system_*` | info, **board_status** (whole-board snapshot), ping, free heap, reset, sleep, set name |
| `gpio_*` | pin mode, read/write, read-all, batch write, **status** (level + mode + PWM) |
| `adc_*` / `dac_*` / `touch_*` | analog in, DAC out + cosine, capacitive touch |
| `pwm_*` | LEDC PWM, tone, servo |
| `i2c_*` / `spi_*` / `uart_*` | the serial buses (scan, read/write, transfer) |
| `wifi_*` | scan, station connect, AP mode, status |
| `nvs_*` | persistent key/value store (str/int/bytes) |
| `fs_*` | LittleFS / SD files (list, read, write, stat, …) |
| `onewire_*` / `espnow_*` / `can_*` | 1-Wire, ESP-NOW, CAN bus |
| `rmt_*` / `i2s_*` | pulse trains (NeoPixel/IR/DHT timing) and PCM audio in/out |
| `watch_*` | on-device rules: the board samples a pin and acts on it by itself |
| `mcpwm_*` / `eth_*` / `camera_*` / `ota_*` | motor PWM, Ethernet, camera JPEG, firmware update |

Conventions the tools follow:

- **Pins** are integers (the chip's GPIO numbers). Set `gpio_mode` to `output`
  before `gpio_write`, and to an input mode before `gpio_read`.
- **Raw bytes** are passed and returned as **hex strings** — `"01ff"`,
  `"01 ff"`, or `"01:ff"` all parse. This applies to I2C/SPI/UART/1-Wire/
  ESP-NOW/CAN payloads, NVS bytes and binary file contents.
- **Capabilities vary by chip.** Call `system_info` first and check
  `capabilities`; DAC, touch, CAN, camera, Ethernet etc. report a clear error on
  boards/firmware that lack them.

`camera_capture` returns the JPEG as an MCP image so vision-capable models can
see it directly.

> What is intentionally **not** a tool: anything that hands back a live object
> (raw TCP/UDP sockets, BLE GATT sessions) or takes a host callback (edge
> interrupts, RX handlers, RMT capture) — neither maps onto request/response
> tool calls. Use the Python `Bridge` API for those.

## Live feedback

Every tool reports what it did on the board — e.g.
`gpio_mode(pin=2, mode='output')`, or `gpio_write(pin=2, value=1) -> 1`. Each
message goes to two
places:

- the **server log** (stderr) — visible to whoever runs `espbridge-mcp`;
- an **MCP notification** — shown by clients that display server logs, so you
  watch board activity live alongside the agent.

Successful actions are reported as **info**; failed ones (e.g. a write to a bad
pin) as **warnings** — so *every* action shows up, not just the ones that worked.

It's **on by default**. Turn it off — start with `espbridge-mcp --no-feedback`,
or at runtime ask the agent to call `bridge_feedback(enabled=false)` (and
`true` to turn it back on).

## Embed it

Build the server in your own process — register extra tools, pick the transport,
or share a `Bridge` you already manage:

```python
from espbridge.mcp import build_server

server = build_server(port="COM7")          # a fastmcp.FastMCP instance

@server.tool
def blink(pin: int, times: int = 3) -> str:
    """Blink an LED a few times."""
    from espbridge.mcp.server import BridgeManager  # or capture your own manager
    ...

server.run()                                # stdio; or run(transport="http", ...)
```

For full control over the connection lifecycle, construct a `BridgeManager`
yourself and pass it in:

```python
from espbridge.mcp import BridgeManager, build_server

mgr = BridgeManager(port="COM7", upgrade_baud=True)
build_server(mgr).run()
```
