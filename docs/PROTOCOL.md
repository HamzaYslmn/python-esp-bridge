# python-esp-bridge wire protocol (v1)

Binary protocol over USB serial between a host (PC / Raspberry Pi) and the
bridge firmware on an ESP32. Optimized for speed and simplicity: no JSON, no
base64, no compression.

Canonical constants live in [`firmware/src/espbridge/commands.h`](../firmware/src/espbridge/commands.h) and
[`src/espbridge/constants.py`](../src/espbridge/constants.py) — those two files
must stay identical in meaning.

## Framing

Logical frame:

| field   | size | notes                                        |
|---------|------|----------------------------------------------|
| flags   | u8   | bit0 `EVENT`, bit1 `ERROR`, bit2 reserved     |
| seq     | u8   | request/response correlation; `0` = no reply |
| cmd     | u16 BE | `(module << 8) \| op`                      |
| payload | 0..2048 | command-specific                          |
| crc16   | u16 BE | CRC-16/CCITT-FALSE over flags..payload     |

The logical frame is **COBS-encoded** and terminated with a single `0x00`
delimiter byte. COBS guarantees no `0x00` inside the encoded frame, so a
receiver that joins mid-stream (or after the ESP32's DTR/RTS auto-reset)
re-synchronizes at the next delimiter. CRC16 catches corruption that framing
alone cannot — there is no hardware flow control on CP2102/CH340 links.

- Max payload **2048 bytes**. Larger transfers (SPI bursts, socket sends) are
  chunked by the host library. The cap keeps firmware RAM bounded (the frame
  buffers must coexist with the Wi-Fi/BLE stacks) and single-frame latency low.
- Multi-byte integers are **big-endian** throughout.
- Strings are length-prefixed: `len u8 | bytes` (UTF-8).

## Request / response

- Host sends a request with `seq` 1..255 (cycling). Firmware replies with the
  same `cmd` and `seq`.
- `seq = 0` means *fire-and-forget*: firmware must not reply
  (used for `NET_WINDOW_ACK`).
- Success: flags `ERROR` clear, payload = command-specific result.
- Failure: flags `ERROR` set, payload = `status u8` (see `Status` enum).

## Events

Asynchronous frames from firmware (flags `EVENT` set, `seq = 0`): GPIO edges,
UART RX, Wi-Fi state/scan results, socket data, BLE advertisements, etc.
By convention event ops are `0x80..0xFF`, request ops `0x00..0x7F`.

## Boot handshake

Opening the serial port toggles DTR/RTS, which auto-resets classic ESP32
boards. At the end of `setup()` the firmware emits a **`SYS_READY` event**
whose payload equals `SYS_INFO`. The host:

1. opens the port at **115200** and discards bytes until a valid `SYS_READY`
   frame (timeout ~3 s; on miss it pulses DTR/RTS to force a reset, then
   falls back to `SYS_PING`),
2. verifies `proto_ver == PROTOCOL_VERSION` (mismatch → reflash needed),
3. stores the capability bitmask (`DAC`, `TOUCH`, `NATIVE_USB`, …) so
   unsupported features fail locally with a clear error.

## Baud negotiation

Link starts at 115200. If the board does **not** have `CAP_NATIVE_USB`
(native USB CDC ignores baud), the host sends `SYS_SET_BAUD(target)`.
Firmware replies OK **at the old baud**, flushes, then switches. The host
switches and re-pings (3 attempts); on failure it drops back to 115200.
Defaults: 921600 (safe for CP2102/CH340/CH9102); CH340 supports up to
2,000,000 opt-in.

## Transports

The frame stream is transport-agnostic; two links carry it today:

**USB serial** — the default. Boot handshake and baud negotiation as above.

**Bluetooth (BLE)** — a Nordic-UART-style GATT service, no USB cable needed:

| | UUID |
|---|---|
| service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` |
| RX (host → board) | `6e400002-…` write / write-no-response |
| TX (board → host) | `6e400003-…` notify, chunked to ATT MTU |

Boards advertise the service plus the name `espbridge_<mac>` (or
`espbridge_<mac>_<name>` once a custom name is stored via `SYS_SET_NAME` —
the advertised name updates on the next reset), so hosts can discover,
display and address every bridge without connecting.

Frames are byte-identical to the serial link; COBS `0x00` delimiters make MTU
chunk reassembly trivial. `SYS_SET_BAUD` is meaningless over BLE (skip it).

### Wireless authentication (`SYS_AUTH`)

USB implies physical access and needs no password. Over BLE, every command
except `SYS_AUTH` is rejected with `ST_DENIED (0x0D)` until the client sends
`SYS_AUTH` with the password as payload (firmware default `"espbridge"`,
compiled in via `BRIDGE_PASSWORD` at the top of `firmware.ino`; empty string
= open access). On success the firmware replies OK and then emits the
`SYS_READY` banner to that client, so the handshake proceeds exactly like
USB. Auth state is per-connection and resets on disconnect.

## NET flow control (socket proxy)

The serial link is slower than Wi-Fi, so each proxied TCP socket has a credit
window (default 4096 bytes). Firmware forwards at most `window` un-acked bytes
as `NET_DATA_EVT` chunks (≤512 B each); when credit is exhausted it stops
reading the TCP socket, letting normal TCP backpressure throttle the peer
(lossless). The host replenishes credit with fire-and-forget
`NET_WINDOW_ACK(handle, n)` as the application consumes data.
UDP is drop-newest on overflow (datagrams are lossy by nature).

## Firmware task architecture (FreeRTOS)

The firmware is built on FreeRTOS tasks, pinned to the app core (the Wi-Fi/BT
stacks own core 0 on dual-core chips):

| task         | prio | role |
|--------------|------|------|
| `bridge_tx`  | 12   | sole serial writer; drains the outbound frame queue |
| `bridge_rx`  | 10   | decodes frames; runs fast handlers (SYS/GPIO/ADC/DAC/TOUCH/PWM/I2C/SPI/UART) inline; pumps GPIO edge + UART RX events |
| `bridge_net` | 9    | owns all Wi-Fi/NET/ESP-NOW/BLE state; executes their (possibly blocking) handlers from a request queue; polls sockets and scan results |

Consequences visible to the host:

- A blocking operation (TCP connect ≤5 s, BLE connect) **only** delays other
  WIFI/NET/BLE commands; GPIO/I2C/SPI/`SYS_PING` stay fully responsive.
- Replies and events can be produced from any task (GPIO ISRs, Bluedroid and
  Wi-Fi callbacks included) — everything funnels through the TX queue, so
  frames never interleave.
- **Responses across modules may arrive out of request order** (e.g. a GPIO
  reply overtakes an in-flight Wi-Fi connect reply). The host must correlate
  by `seq`, never by arrival order. Within one module, order is preserved.

## Command reference

See the comment beside every id in `firmware/src/espbridge/commands.h` for exact payload layout;
module ids: SYS 0x00, GPIO 0x10, ADC 0x20, DAC 0x21, TOUCH 0x22, PWM 0x30,
RMT 0x31, MCPWM 0x33, I2C 0x40, SPI 0x41, UART 0x42, ONEWIRE 0x43, TWAI 0x44,
I2S 0x45, WIFI 0x50, NET 0x51, ESPNOW 0x52, ETH 0x53, BLE 0x60, FS 0x70,
NVS 0x71, OTA 0x72, CAM 0x73.

Every v0.3.0 module is gated on its own capability bit (`CAP_RMT` …
`CAP_SLEEP`), so hosts probe `SYS_INFO.caps` instead of guessing from the
firmware version.

### RMT — generic pulse trains (module 0x31)

The design rule of v0.3.0: the firmware moves *symbols*, the host implements
*device protocols*. A symbol is `u16 BE = level<<15 | duration` in ticks of
`1/tick_hz` (set at `RMT_INIT`, 1 kHz..80 MHz). One primitive set covers
WS2812 strips, IR send/receive, DHT, HC-SR04 and stepper pulse generation —
all decoding/encoding lives in Python (`espbridge.neopixel/ir/dht/hcsr04/stepper`).

| cmd | payload | reply |
|-----|---------|-------|
| `RMT_INIT` 0x01 | `pin u8 \| dir u8 (0 tx, 1 rx) \| tick_hz u32` | ok |
| `RMT_DEINIT` 0x02 | `pin u8` | ok |
| `RMT_TX` 0x03 | `pin u8 \| sym u16[..]` | ok (after the train is sent) |
| `RMT_TX_BYTES` 0x04 | `pin u8 \| bit0 u32 \| bit1 u32 \| data..` | ok — each byte expands MSB-first into per-bit symbol pairs (WS2812) |
| `RMT_TX_LOOP` 0x05 | `pin u8 \| sym u16[..]` | ok; repeats until TX_STOP |
| `RMT_TX_STOP` 0x06 | `pin u8` | ok |
| `RMT_RECV` 0x07 | `pin u8 \| idle u16 \| timeout_ms u16 \| max_syms u16 \| trig_pin u8 \| trig_level u8 \| trig_us u32` | captured `sym u16[..]` (empty = timeout) |
| `RMT_CARRIER` 0x08 | `pin u8 \| freq u32 \| duty_pct u8 \| enable u8` | ok |

`RMT_RECV` arms the receiver **before** firing the optional trigger pulse, so
single-wire request/response sensors work: DHT uses `trig_pin == pin`
(open-drain start signal), HC-SR04 a separate trigger pin, IR receive no
trigger. Captures are capped at `RMT_MAX_RX_SYMS` (1020).

### 1-Wire (module 0x43)

Bit-timing primitives only — ROM search, CRC8 and device drivers live in
Python (`espbridge.onewire`, `espbridge.ds18b20`): `OW_RESET` (presence),
`OW_WRITE` (with optional strong pull-up for parasite power), `OW_READ`,
`OW_TRIPLET` (one Maxim-search step: read bit + complement, write direction).

### TWAI / CAN (module 0x44)

`TWAI_INIT` (pins, mode, baud preset 25k–1M, optional acceptance filter —
the IDF driver only takes the filter at install time), `TWAI_SEND`
(id + ≤8 B, queued), `TWAI_STATUS` (error counters / bus-off), `TWAI_RECOVER`,
`TWAI_DEINIT`. Received frames stream as `TWAI_RX_EVT 0x80`. Needs an
external transceiver chip.

### I2S (module 0x45)

`I2S_INIT` (direction, pins, rate, bits, mono/stereo), then pull/push PCM with
`I2S_READ`/`I2S_WRITE` in ≤2 KB chunks. The link caps usable rates: ~92 KB/s
at 921600 baud ⇒ 16-bit mono up to ~32 kHz; 44.1 kHz stereo does not fit.

### FS (module 0x70) and NVS (module 0x71)

FS: LittleFS (id 0, auto-formats), SD over SPI (id 1), SDMMC (id 2, where the
SoC has the host). `FS_OPEN/READ/WRITE/SEEK/CLOSE` on a small fd table,
`FS_LIST` streams entries as `FS_LIST_EVT` with the reply (= entry count) as
the done marker, plus STAT/REMOVE/RENAME/MKDIR/DF. NVS: raw-bytes key/value
in a dedicated `user` namespace (`NVS_SET/GET/DEL/KEYS/CLEAR`); typed
encoding is the host's job.

### OTA (module 0x72)

`OTA_BEGIN(size)` → `OTA_WRITE` (1 KB chunks, reply = cumulative count =
progress) → `OTA_END(commit)` which reboots into the new image. Works over
**USB and BLE**. Requires a dual-app partition table ("Minimal SPIFFS" on
4 MB flash); on the no-OTA "Huge APP" table `OTA_BEGIN` replies
`ST_UNSUPPORTED`.

### Sleep (SYS 0x08/0x09)

`SYS_SLEEP` (deep or light, timer µs and/or GPIO wake). Deep sleep replies
OK, flushes, then powers down — the board reboots on wake. Light sleep
replies *after* waking with the cause. `SYS_WAKE_CAUSE` reports the last
boot's wake reason. Gated on `CAP_SLEEP` (see IRAM note below).

### ETH (module 0x53) and CAM (module 0x73) — compile-time opt-ins

Disabled by default (`BRIDGE_ENABLE_ETH` / `BRIDGE_ENABLE_CAM` in
firmware.ino). Board specifics live on the host: `espbridge.eth.PRESETS`
(WT32-ETH01, Olimex POE, W5500-SPI…) and `espbridge.camera.PRESETS`
(AI-Thinker ESP32-CAM, XIAO-S3-Sense…) send pin maps over the wire. Once
Ethernet has an IP, all NET sockets ride it automatically (unified core-3.x
Network stack). Camera frames are captured on-board and read out in ≤2 KB
chunks (`CAM_CAPTURE` → `CAM_READ` → `CAM_RELEASE`); `CAM_SET` maps to
`sensor_t` tuning calls with host-defined property ids.

### ESP-NOW (module 0x52)

Connectionless ESP32-to-ESP32 messaging (≤250 B per packet), gated on the
`CAP_ESPNOW` capability bit.

| cmd | payload | reply |
|-----|---------|-------|
| `ESPNOW_INIT` 0x01 | `channel u8` (0 = auto/inherit) \| `flags u8` (bit0 = long-range PHY) | own STA `mac[6]` |
| `ESPNOW_DEINIT` 0x02 | — | ok |
| `ESPNOW_SET_PMK` 0x03 | `pmk[16]` | ok |
| `ESPNOW_ADD_PEER` 0x04 | `mac[6]` \| `channel u8` (0 = follow) \| `encrypt u8` \| `[lmk[16]]` | ok |
| `ESPNOW_DEL_PEER` 0x05 | `mac[6]` | ok |
| `ESPNOW_SEND` 0x06 | `mac[6]` \| `data..` (≤250 B) | `delivered u8` (1 = peer's radio ACKed) |
| `ESPNOW_RX_EVT` 0x80 | `src_mac[6]` \| `rssi i8` \| `data..` | event |
| `ESPNOW_SEND_EVT` 0x81 | `dst_mac[6]` \| `status u8` (0 = delivered) | event |

`ESPNOW_SEND` has two lanes: with `seq != 0` the firmware blocks (≤25 ms) on
the radio's TX callback and replies with the real delivery result; with
`seq == 0` (fire-and-forget) it returns immediately and the result is emitted
later as a best-effort `ESPNOW_SEND_EVT` — use it for max-rate streaming.
Broadcasts (`ff:ff:ff:ff:ff:ff`, registered as a normal peer) are never ACKed.

`ESPNOW_INIT` auto-starts the Wi-Fi driver in STA mode when it is off. While
the board is associated to a Wi-Fi network (or running an AP), ESP-NOW
**inherits that channel** and the requested channel is ignored (a `SYS_LOG`
warning is emitted) — switching would drop the association. All peers must
share a channel to hear each other. Encryption: `ESPNOW_SET_PMK` once, then
per-peer 16-byte LMKs (both sides must match).

### Device identity (multi-device setups)

`SYS_SET_NAME` (payload = 0..32 raw bytes) persists a user-assigned name in
the ESP32's NVS flash. The name is appended to the `SYS_INFO` / `SYS_READY`
payload as a length-prefixed string tail, letting hosts pick a specific board
(`Bridge(name="relays")`) regardless of which serial port it enumerated on.
Hosts must treat the tail as optional for compatibility with older firmware.

### Known quirks encoded in the protocol

- **ADC2 vs Wi-Fi** (classic ESP32): ADC2 pins (GPIO 0, 2, 4, 12–15, 25–27)
  return `ST_BUSY` while Wi-Fi is active.
- **DAC** exists only on classic ESP32 (GPIO 25/26); elsewhere `ST_UNSUPPORTED`.
- **Bootstrap pins** (GPIO 0, 2, 12, 15): `GPIO_SET_MODE` succeeds but emits a
  `SYS_LOG` warning event.
- **SYS_RESET** replies OK first, then restarts; the host must expect a new
  `SYS_READY`.
- **Radio coexistence** (classic ESP32): the Wi-Fi stack must initialize
  before Bluedroid, so the firmware brings the Wi-Fi driver up at boot when
  the BLE link is enabled (`BRIDGE_WIFI_COEX` in firmware.ino, default on;
  costs ~50 KB heap). Power save stays at the IDF default `WIFI_PS_MIN_MODEM`
  — never `WIFI_PS_NONE` while BT is active.
- **Wi-Fi scan vs ESP-NOW**: a scan hops channels, so ESP-NOW packets are
  dropped while one is running.
- **IDF logs become `SYS_LOG` events**: the Wi-Fi/BT stacks log through
  `esp_log`; on UART links those bytes would corrupt COBS frames, so the
  firmware redirects them into `SYS_LOG` (level 1) events. ROM boot output and
  panic dumps still hit UART0 raw. Native-USB chips keep IDF logs on UART0.
- **Stale BLE bonds** (Bluedroid NVS): switching a board between BLE-using
  firmwares can leave NVS namespaces that assert at boot — do a one-time
  "Erase All Flash" when flashing over unknown firmware.
- **Classic-ESP32 IRAM budget**: with Wi-Fi + Bluedroid loaded there is
  ~1.7 KB of instruction RAM to spare. The SD drivers (~4.4 KB of IRAM ISRs)
  and the IDF sleep API (~1.7 KB) don't fit, so on classic ESP32 with the BLE
  link compiled in: `CAP_SLEEP` is absent and FS offers LittleFS only.
  Building with `BRIDGE_ENABLE_BLE 0` frees the BT IRAM and restores SD,
  SDMMC and sleep. Other chips are unaffected.
- **A long RMT capture or FS/OTA burst runs on the same task as Wi-Fi/NET**:
  it can delay those replies (never GPIO/I2C/SPI, which stay on the rx task).
