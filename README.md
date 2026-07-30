# python-esp-bridge

[Türkçe README](docs/README-TR.md)

Connect an ESP32 to a Raspberry Pi (or any PC) over USB **or Bluetooth** and
drive **every** ESP32 peripheral live from Python — GPIO, PWM, ADC, DAC,
capacitive touch, I2C, SPI, extra UARTs, RMT pulse trains (NeoPixels, IR,
DHT, ultrasonic, steppers), 1-Wire, CAN bus, I2S audio, files (LittleFS/SD),
NVS storage, deep sleep, Wi-Fi (including TCP/UDP sockets through the ESP32
radio), Ethernet, camera, BLE and ESP-NOW — plus firmware updates **over the
link itself**. Flash the bridge firmware **once**; after that, everything is
Python on the host. No reflashing per project.

The design rule: the firmware exposes minimal hardware primitives; device
protocols (WS2812 timing, NEC IR, DHT decoding, 1-Wire search, stepper ramps)
are implemented in Python where they are easy to read, test and extend.

```
┌────────────────┐  USB serial (≤2 Mbaud) or BLE    ┌─────────────────────┐
│ Pi / PC        │ ───────────────────────────────► │ ESP32 (bridge fw)   │
│ Python:        │   binary protocol, COBS+CRC16    │ FreeRTOS tasks:     │
│  espbridge     │ ◄─────────────────────────────── │  tx / rx / network  │
└────────────────┘        replies + async events    └─────────────────────┘
```

![Oled](docs/img/oled.png)

## Quick start

*Works on Raspberry Pi OS, Linux, Windows, and macOS (requires Python ≥ 3.11).*

1. **Flash the firmware once.** No Arduino IDE needed — flash the bundled
   prebuilt firmware straight from the host (lists the serial ports, you pick
   one, it writes a *Huge APP* image with esptool):

   ```sh
   uvx --from "python-esp-bridge[flash]" flash   # zero-install via uv
   ```

   Prefer building it yourself? Install the **`python esp bridge`** library
   (Arduino IDE Library Manager), open *File → Examples → python esp bridge →
   Bridge*, pick partition scheme *Huge APP*, hit Upload. The whole sketch is
   three lines: `EspBridge.usb.begin(); EspBridge.ble.begin(); EspBridge.run();`
   Details: [`docs/FIRMWARE.md`](docs/FIRMWARE.md).
2. **Install the Python library** on the Pi/PC — with pip:

   ```sh
   pip install python-esp-bridge            # USB + Bluetooth, both included
   pip install "python-esp-bridge[oled]"    # + Pillow, for OLED displays
   pip install "python-esp-bridge[mcp]"     # + the MCP server (espbridge-mcp)
   pip install "python-esp-bridge[flash]"   # + esptool, for `espbridge flash`
   ```

   ...or with [uv](https://docs.astral.sh/uv/):

   ```sh
   uv add python-esp-bridge                 # into a uv project (USB + Bluetooth)
   uv add "python-esp-bridge[oled]"         # with an extra (oled / mcp / all)
   uv pip install python-esp-bridge         # ...or into the active environment
   ```

   Bluetooth works out of the box (no extra); the old `[ble]` extra is kept as
   a no-op for back-compat.

3. **Go:**

   ```python
   from espbridge import Bridge

   with Bridge() as esp:                      # USB first, then Bluetooth, then Wi-Fi
       print(esp.info)                        # chip, MAC, capabilities

       esp.gpio.mode(2, "output")             # like RPi GPIO, but on the ESP32
       esp.gpio.write(2, 1)                    # returns the pin's read-back level
       esp.gpio.write(2, 1, verify=True)      # ...and raises if it didn't take
       print(esp.adc.read_mv(34), "mV")
       esp.dac.write(25, 128)                 # true analog out (classic ESP32)
       esp.pwm.servo(13, angle=90)

       esp.i2c.init(sda=21, scl=22)
       print([hex(a) for a in esp.i2c.scan()])

       esp.wifi.connect("ssid", "password")   # the ESP32's radio...
       status, body = esp.net.http_get("http://example.com/")  # ...as your modem
   ```

   Or with **no USB cable at all** — boards advertise as `espbridge_<name>` and
   require a password (default `espbridge`, change it via
   `EspBridge.ble.begin("yourpassword")` in the sketch):

   ```python
   with Bridge(ble=True, password="espbridge") as esp:   # over Bluetooth
       esp.gpio.write(2, 1)
   ```

   `Bridge()` takes the best link available, in that order: a **USB** cable if
   one is plugged in, else **Bluetooth**, else the board's **Wi-Fi** address.
   Each transport keyword pins its link instead of merely preferring it:
   `ble=False` is **USB / COM only**, `ble=True` Bluetooth only, `wifi=True`
   Wi-Fi only — ask for a link and that is the link you get, or an error.
   `Bridge("relays")` picks one specific board over any of them, `port="COM7"`
   one specific serial port.

   `espbridge` on the command line prints connection info; `espbridge ports`
   lists candidate serial ports; `espbridge scan` probes every attached board
   and `espbridge scan --ble` finds bridges advertising over Bluetooth.

## Features

| module | highlights |
|--------|------------|
| GPIO   | modes incl. pull-up/down & open-drain, batch writes, writes return the pin's read-back level for confirmation (`verify=` raises on mismatch), edge interrupts with debounce → Python callbacks |
| ADC    | raw + calibrated mV, attenuation config (ADC2/Wi-Fi conflict guarded) |
| DAC    | 8-bit output + hardware cosine generator (classic ESP32 / S2) |
| PWM    | LEDC: any pin, freq/resolution, `duty_pct`, `tone`, `servo` |
| Touch  | capacitive touch pad reads |
| I2C    | 2 buses, scan, write/read, register helpers, repeated-start |
| SPI    | 2 hosts, full-duplex transfers, CS handling |
| UART   | UART1/2 bridged: write from Python, RX streamed back as events |
| Wi-Fi  | scan, STA join, AP mode, status/RSSI, state events |
| NET    | TCP client/server + UDP **through the ESP32 radio**, socket-like API, credit-window flow control |
| BLE    | scan, advertise, GATT server (notify/write callbacks), GATT client |
| ESP-NOW | connectionless ESP32↔ESP32 messaging: peers + broadcast, delivery ACKs, RX with RSSI, PMK/LMK encryption — coexists with Wi-Fi and BLE |
| RMT    | generic pulse-train play/capture — the one primitive behind `neopixel`, `ir`, `dht`, `hcsr04`, `stepper` (below) |
| 1-Wire | bus primitives on any pin; ROM search + CRC8 in Python (`esp.onewire`, DS18B20 driver included) |
| CAN    | TWAI controller: 25k–1M bit/s, filters, send/recv + callbacks (`esp.can`; transceiver chip required) |
| I2S    | PCM in/out for MEMS mics & DACs/amps (`esp.i2s`; link bandwidth caps rates ~16-bit/32 kHz mono) |
| Files  | LittleFS on internal flash + SD cards: open/read/write/list/… (`esp.fs`) |
| NVS    | persistent key/value storage on the board (`esp.nvs`) |
| Watch  | on-device rules: the board samples ADC/GPIO/touch/heap itself and pushes an event when a condition trips — and can **react on-device** (`do=("gpio", pin, level)` / `do=("pwm", pin, duty)`) in ~5 ms, link-independent, even mid-BLE-dropout |
| Sleep  | deep + light sleep with timer/GPIO wake (`esp.deep_sleep()`; see chip notes) |
| Power  | `esp.radio_off()`: Wi-Fi + ESP-NOW + the whole BT stack off — sheds all radio interrupts, frees ~110 KB heap, unlocks the ADC2 pins for jitter-sensitive realtime work over USB; `esp.cpu_freq()`, `esp.power_mode()` |
| OTA    | **reflash the firmware over USB or Bluetooth** (`esp.ota.flash("fw.bin")`; dual-app partition scheme) |
| Ethernet | RMII (WT32-ETH01, Olimex POE…) or SPI (W5500) — NET sockets ride it automatically (firmware opt-in) |
| Camera | JPEG snapshots from ESP32-CAM / XIAO-S3-Sense / ESP-EYE (firmware opt-in, PSRAM) |
| MCPWM  | complementary PWM pair with hardware deadtime for H-bridges (`esp.mcpwm`; not on S2/C3) |

**Device drivers in pure Python** (over the RMT/1-Wire/I2C primitives — no
firmware changes to add your own):

```python
from espbridge.drivers.neopixel import NeoPixel   # WS2812/SK6812 strips
from espbridge.drivers.dht import DHT             # DHT11/DHT22 temp+humidity
from espbridge.drivers.ds18b20 import DS18B20     # 1-Wire thermometers (multi-drop)
from espbridge.drivers.hcsr04 import HCSR04       # ultrasonic ranging
from espbridge.drivers.ir import IrSender, IrReceiver  # NEC remotes + raw IR
from espbridge.drivers.stepper import Stepper     # A4988/DRV8825 with ramps

NeoPixel(esp, pin=5, n=30).fill((0, 0, 64))
print(DHT(esp, 4).read())                 # (23.1, 65.5)
Stepper(esp, step_pin=12, dir_pin=14).move(400, speed=800, accel=1600)
```

### Bring your own driver

Those are **reference implementations, not the limit.** Every bundled driver
lives in [`espbridge/drivers/`](python/espbridge/drivers/), and a driver is just
a Python class whose constructor takes the bridge and talks to a device over the
primitives above — [`drivers/dht.py`](python/espbridge/drivers/dht.py) is 75
lines. So any sensor, display or protocol is a host-side class away, with **no
firmware change**:

```python
class MyTempSensor:                          # any class taking the bridge first
    def __init__(self, esp, address=0x48):
        self._i2c, self._addr = esp.i2c, address
    def read_c(self):
        hi, lo = self._i2c.read_reg(self._addr, 0x00, 2)
        return ((hi << 8 | lo) >> 4) * 0.0625

MyTempSensor(esp).read_c()                    # works as-is, nothing to register
```

Register a name for the `esp.<name>(...)` sugar, or ship a pip package that
others install and your driver shows up on every bridge automatically:

```python
from espbridge import register_driver
register_driver("mytemp", MyTempSensor)
esp.mytemp(address=0x48).read_c()             # == MyTempSensor(esp, address=0x48)
```

`espbridge drivers` lists everything available (the bundled
[`drivers/`](python/espbridge/drivers/) and any installed plugins). Full guide:
[**`docs/DRIVERS.md`**](docs/DRIVERS.md). And if a driver already exists in the
Adafruit / luma / gpiozero / smbus2 ecosystems, it runs unchanged through the
[compat shims](#use-the-libraries-you-already-know) — no rewrite needed.

The firmware is fully event-driven on FreeRTOS: serial TX, command handling
and the network stack run as separate tasks, so a blocking Wi-Fi/BLE
operation never delays a GPIO read (~1 ms round-trips; the link auto-upgrades
from 921600 Bd to what the USB bridge chip supports — 1.5 Mbaud on CP210x,
2 Mbaud on CH340).

## Concurrency & integration

A board's link can't be opened twice, but **one `Bridge` is thread-safe** —
share it across threads and their requests pipeline on the wire, correlated by
sequence number, so a slow call on one thread never stalls a fast call on
another (the firmware runs a matching task split; see
[`rtos_concurrency.py`](python/examples/basics/rtos_concurrency.py)).

For easy integration, don't pass a `Bridge` around — call `connect()` anywhere
and get the same shared, auto-reconnecting link:

```python
import espbridge

esp = espbridge.connect(ble=False)      # same live link from any thread/module
esp.gpio.write(2, 1)                     # safe to call concurrently

# e.g. a FastAPI/Flask route — every request shares the one connection:
@app.get("/adc/{pin}")
def read(pin: int):
    return {"mV": espbridge.connect(ble=False).adc.read_mv(pin)}
```

For `await`, wrap any bridge — fan out concurrent I/O with `asyncio.gather`
([`async_fanout.py`](python/examples/basics/async_fanout.py)):

```python
from espbridge import AsyncBridge

async with AsyncBridge(ble=False) as esp:        # or AsyncBridge.wrap(espbridge.connect())
    t, h = await asyncio.gather(esp.adc.read(34), esp.adc.read(35))
```

Multiple processes? One process owns the link (e.g. the
[MCP](#drive-it-from-an-ai-agent-mcp) or an HTTP server) and the others talk to
it. See [`shared_connection.py`](python/examples/basics/shared_connection.py).

## Use the libraries you already know

espbridge speaks the wire protocols of the popular Python hardware ecosystems,
so existing code, drivers and tutorials run unchanged — the ESP32's pins just
take the place of the Pi's:

**gpiozero** — full pin factory (LED, Button, PWMLED, edge callbacks, …):

```python
from gpiozero import LED, Button
from espbridge.compat.gpiozero import EspBridgeFactory

factory = EspBridgeFactory(esp)
led, btn = LED(2, pin_factory=factory), Button(4, pin_factory=factory)
btn.when_pressed = led.toggle
```

**Adafruit CircuitPython drivers** (hundreds of sensors/displays) — busio/digitalio-compatible I2C, SPI and DigitalInOut:

```python
from adafruit_bme280.basic import Adafruit_BME280_I2C
from espbridge.compat.blinka import I2C

bme = Adafruit_BME280_I2C(I2C(esp))     # the driver doesn't know it's bridged
print(bme.temperature)
```

**smbus2** — classic Pi I2C code, unchanged:

```python
from espbridge.compat.smbus import SMBus
bus = SMBus(esp)                        # instead of smbus2.SMBus(1)
temp = bus.read_byte_data(0x48, 0x00)
```

**luma.oled / luma.lcd** — I2C and SPI display interfaces (`LumaI2C`, `LumaSPI`),
**RPi.GPIO** — `espbridge.compat.rpi_gpio` shim, and the native objects follow
stdlib conventions too: UART ports are pyserial-like (`in_waiting`, `readline`),
bridged TCP/UDP sockets support `settimeout`/`recv`/`sendall`.

I2C OLEDs (SSD1306 / SH1106 / the ubiquitous clones) are supported directly —
`pip install "python-esp-bridge[oled]"`, draw with PIL:

```python
from espbridge.drivers.oled import OLED

oled = OLED(esp)                # bus init + auto-detect + clone-safe power-up
with oled.draw() as d:          # d is a PIL ImageDraw
    d.text((0, 10), "Hello!", fill="white")
```

## Drive it from an AI agent (MCP)

Expose the whole bridge to an LLM as a [Model Context Protocol](https://modelcontextprotocol.io)
server — 100+ tools covering every peripheral (GPIO, ADC/DAC, PWM, I2C, SPI,
UART, Wi-Fi, NVS, filesystem, 1-Wire, ESP-NOW, CAN, MCPWM, Ethernet, camera,
OTA). The agent can then read sensors, toggle pins, scan I2C and more in plain
language.

Install the server once, then plug in the board — the port auto-detects:

```bash
uv tool install "python-esp-bridge[mcp]"     # or: pip install "python-esp-bridge[mcp]"
```

Works with **Claude Code, Gemini CLI, Codex CLI, Antigravity, Cursor/Windsurf
and Ollama** — all launch the same `espbridge-mcp` command. This repo ships
ready configs for Claude Code (`.mcp.json`) and Gemini CLI
(`.gemini/settings.json`): just open the assistant in the repo. Every other
client uses the same one-line config (key `mcpServers`):

```jsonc
{ "mcpServers": {
    "espbridge": { "command": "espbridge-mcp", "args": [] }
} }
```

Tools are grouped by peripheral (`gpio_*`, `i2c_*`, `wifi_*`, …); raw byte
payloads go in and out as hex strings. Embed it in your own server with
`from espbridge.mcp import build_server`. **Per-assistant setup (incl. Codex,
Antigravity, Ollama): [`docs/MCP.md`](docs/MCP.md).**

### Multiple ESP32s

Name each board once (`espbridge -p COM7 set-name relays` — stored in its flash,
survives reboots and port renumbering), then never think about ports or MACs
again:

```python
from espbridge import Bridge

esp = Bridge()                            # one board  -> whichever answers
esp = Bridge("relays")                    # one name   -> that one board
esp = Bridge("c0:49:ef:d0:3f:e0")         # a MAC works too, same argument

with Bridge.all(["relays", "sensors"]) as boards:   # exactly those
    boards["relays"].gpio.write(2, 1)

with Bridge.all() as boards:              # every board on the desk
    boards.each(lambda esp: esp.ping())
```

`Bridge()` is one board and `Bridge.all()` is every board — nothing about how
you call it changes the type you get back.

That argument is the board's **identity** — its name, or its MAC if you never
named it — and it works the same over USB, Bluetooth and Wi-Fi, because both
halves travel in the Bluetooth advertisement, the Wi-Fi discovery reply and
`SYS_INFO` alike. It is never a COM port or an IP address; those have their own
keywords, so nothing is guessed from the shape of the string.

Names are capped at 16 characters, which is what keeps the advertised
`espbridge_<name>` inside the 26 the Bluetooth scan response fits — a name that
would be cut short over the air is refused instead. Asking for boards that
aren't all there is an error, not a partial run.

### Over Wi-Fi

The same protocol runs over TCP, so a board needs neither a cable nor
Bluetooth range. Provision it once — credentials live in the board's flash, so
it rejoins on every boot with no reflash and no password in your sketch:

```python
with Bridge(port="COM3") as usb:
    usb.wifi.link_setup("my-ssid", "my-password")   # board joins and listens

esp = Bridge(wifi=True)              # found by UDP broadcast on the LAN
esp = Bridge(host="192.168.1.50")    # ...or address it directly
```

Round trips run ~7-8 ms (versus 2 ms over USB), and the board answers a few
hundred requests a second. Keep Bluetooth off on a latency-sensitive board:
Wi-Fi modem sleep has to stay enabled while BLE is up, and a classic ESP32
running both is down to ~10 KB of free heap.

### However many boards there are

One extra call covers every scale: `Bridge.all()` opens all of them, and a list
opens exactly the ones you name — over USB, or over Wi-Fi with `wifi=True`:

```python
from espbridge import Bridge

with Bridge.all(wifi=True) as boards:
    boards.wait_for(800, timeout=120)                  # they keep arriving
    boards.each(lambda esp: esp.ping())                # all of them, at once

    def blink(esp):                                    # ...or arbitrary work
        esp.gpio.mode(2, "output")
        esp.gpio.write(2, 1)
    boards.each(blink)                                 # -> {ident: result}

    boards["relays"].oled(addr=0x3c).clear()           # or just pick one
```

Which end opens the socket is a provisioning detail, not something you ask about
here: boards left in listen mode answer a UDP broadcast and get dialled, and
boards told to dial home connect in and go on arriving.

```python
with Bridge(port="COM3") as usb:                       # once per board
    usb.wifi.link_setup("ssid", "pw")                  # listen mode, or...
    usb.wifi.link_setup("ssid", "pw", server="192.168.1.10")   # ...dial home
```

Dialling home is what scales: nothing tracks IP addresses, so DHCP churn, NAT
and reboots stop mattering, and each board reconnects on its own with jittered
backoff. You get the same [`BridgeSet`](#however-many-boards-there-are) as over USB and
every entry is an ordinary `Bridge` — sub-APIs, drivers and `esp.watch` behave
identically. A board that comes back replaces its old entry (matched by MAC), and
one that fails maps to its exception instead of failing the sweep. Full example:
[`python/examples/network/many_boards.py`](python/examples/network/many_boards.py).

## Troubleshooting

Errors name the command and say what to check (`I2C_WRITE (0x4003) failed:
IO — no ACK on the wire — check wiring, power, device address and pull-ups`).
A timeout additionally pings the board so the message tells you whether the
link itself died or a single frame got lost. Useful knobs:

```python
esp = Bridge(retries=1)         # default: re-send safe commands once on timeout
esp.free_heap()                 # heap + dropped-frame counters from the firmware
```

```bash
ESPBRIDGE_DEBUG=1 python app.py   # trace every request/response with names
```

Lost frames on a busy link are also *prevented* now: pipelined bursts
(OLED frames, NeoPixel updates) are automatically throttled to what the
firmware's link buffer can absorb, on both USB serial and Bluetooth.

## Repo layout

The repo root **is** the Arduino library (so it's publishable to the Arduino
Library Manager); the Python package lives under `python/`.

| path | what |
|------|------|
| [`src/`](src/) + [`examples/Bridge/`](examples/Bridge/) | Arduino library — the flash-once firmware (C/C++) + its example sketch (`EspBridge.usb/ble/wifi.begin()`) |
| `library.properties`, `keywords.txt` | Arduino Library Manager metadata (at the repo root, as the registry requires) |
| [`python/`](python/) | Python package `python-esp-bridge` (import `espbridge`) with its own `tests/` and grouped `examples/` (`basics/`, `devices/`, `system/`, `wireless/`, `network/`, `displays/`, `compat/`) |
| [`docs/MCP.md`](docs/MCP.md) | MCP server (`espbridge-mcp`): drive the bridge from an AI agent |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | binary wire protocol spec (framing, transports, auth) |
| [`docs/FIRMWARE.md`](docs/FIRMWARE.md) | firmware flashing, partition scheme & build-flag reference |

## Supported hardware

Primary target: classic **ESP32** DevKits (ESP-32S / ESP-32D, 30- and 38-pin,
CP2102/CH340 USB). **ESP32-S2/S3/C3/C6/H2** build via the same sketch (native USB;
ESP-NOW works everywhere; no DAC on S3/C3/C6/H2). Capabilities are reported by the
firmware at connect time, so the Python API fails fast with a clear error for
anything your chip lacks.

Tested with **arduino-esp32 core 3.3.6** on classic ESP32 (CP2102), flashed
with the **Minimal SPIFFS** partition scheme (1.9 MB app + OTA — the firmware
is ~95% of that slot; `Huge APP` also works if you don't need OTA). Verified:
USB at 1.5 Mbaud, the BLE link, ESP-NOW, and Wi-Fi/BLE/ESP-NOW coexistence —
see `python/examples/wireless/stress_test.py` for the soak/coex suite used.

> **Bluetooth note:** arduino-esp32 core 3.x ships the NimBLE host on
> S3/C3/C6/H2 — the bridge's Bluetooth code (BLE link + `esp.ble`) speaks
> Bluedroid, so on those chips the firmware currently builds USB-only.
> Classic ESP32 keeps Bluedroid: full BLE link + Wi-Fi + ESP-NOW coexistence.

> **Classic-ESP32 IRAM trade-off:** with Wi-Fi + Bluetooth both loaded the
> chip's instruction RAM is full, so the default classic build skips SD-card
> support (LittleFS still works) and deep/light sleep. Build with
> `BRIDGE_ENABLE_BLE 0` (USB-only) to get SD + sleep back; S2/S3/C3/C6/H2 have
> everything regardless. The Python API raises a clear `UnsupportedError`
> either way (`Cap.SLEEP`, `Cap.SDMMC` probing).

### Per-chip support matrix (v0.3.5 modules)

| | ESP32 | S2 | S3 | C3 | C6 | H2 |
|---|---|---|---|---|---|---|
| RMT / 1-Wire / CAN / I2S / NVS / OTA | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| LittleFS | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| SD (SPI) / sleep | BLE off only | ✓ | ✓ | ✓ | ✓ | ✓ |
| SDMMC slot | BLE off only | — | ✓ | — | — | — |
| MCPWM (deadtime pair) | ✓ | — | ✓ | — | ✓ | ✓ |
| Camera (opt-in) | ✓ (PSRAM) | ✓ (PSRAM) | ✓ (PSRAM) | — | — | — |
| Ethernet RMII (opt-in) | ✓ | — | — | — | — | — |
| Ethernet SPI W5500 (opt-in) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
