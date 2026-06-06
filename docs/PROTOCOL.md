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
| `bridge_net` | 9    | owns all Wi-Fi/NET/BLE state; executes their (possibly blocking) handlers from a request queue; polls sockets and scan results |

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
I2C 0x40, SPI 0x41, UART 0x42, WIFI 0x50, NET 0x51, BLE 0x60.

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
