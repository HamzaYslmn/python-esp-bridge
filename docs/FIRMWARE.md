# python esp bridge (Arduino library)

Flash-once ESP32 firmware that exposes **every** peripheral to Python over USB
serial **or Bluetooth**. Install the library, flash the one-line example, then
drive the board live from the host with `pip install python-esp-bridge` — no
firmware edits per project.

```cpp
#include <PythonEspBridge.h>
void setup() { EspBridge.begin(); }   // BLE password "espbridge", Bluetooth on
void loop()  {}                       // never runs — begin() owns the board
```

```python
from espbridge import Bridge
with Bridge() as esp:          # USB
    print(esp.info)            # chip, MAC, fw version, capabilities
with Bridge(ble=True) as esp:  # Bluetooth (default password "espbridge")
    esp.gpio.write(2, 1)
```

GPIO, ADC/DAC, PWM, touch, I2C, SPI, UART, Wi-Fi sockets, BLE, ESP-NOW, RMT,
1-Wire, CAN, I2S, LittleFS/SD, NVS and OTA — all from Python.

## Install

- **Arduino IDE / CLI Library Manager:** search **`python esp bridge`** and
  install. Then open *File → Examples → python esp bridge → Bridge*.
- **Manual:** clone the repo into your `Arduino/libraries/` directory
  (`library.properties` sits at the repo root, so the clone *is* the library).

## Requirements

- **arduino-esp32 core 3.x** (Boards Manager → "esp32 by Espressif Systems").
  The 3.x LEDC/String APIs are required; it will not build on 2.x.
  Tested with **3.3.6** (classic ESP32, Minimal SPIFFS partition scheme).
- No external libraries — Bluetooth uses the bundled Bluedroid stack.
  Note: core 3.3.x ships NimBLE (not Bluedroid) on S3/C3/C6, so the Bluetooth
  link is currently classic-ESP32 only; other chips build USB-only
  automatically (ESP-NOW and everything else still work).

## API

`EspBridge.begin(password = "espbridge", ble = true)` — start the bridge. It
spins up the FreeRTOS task model (separate TX / RX / network tasks so a
blocking TCP or BLE connect never stalls GPIO/I2C/SPI) and **does not return**:
it deletes the Arduino loop task to hand its 8 KB stack back to the heap, which
a classic ESP32 running Wi-Fi + Bluedroid needs badly.

- `password` — secret a Bluetooth client must send via `SYS_AUTH` before any
  command is accepted (`""` = open access). USB never asks for a password.
- `ble` — start the Bluetooth link. Pass `false` for USB-only at runtime
  (ignored on builds compiled without BLE).

## Partition scheme (pick one)

The Wi-Fi + BLE build needs a >1.2 MB app slot, so the **default table won't
fit** — choose one in *Tools → Partition Scheme* at the first USB flash:

| Scheme | App | OTA? | Pick when |
|---|---|---|---|
| **Huge APP (3MB No OTA/1MB SPIFFS)** | 3 MB | no | default — biggest app; you always reflash over USB |
| **Minimal SPIFFS (1.9MB APP with OTA)** | 1.9 MB | yes | you want cable-free firmware updates |

OTA (`espbridge.ota`, USB *or* Bluetooth) updates the firmware binary itself —
the cable-free complement to the link for deployed boards. It needs the OTA
partition above; on Huge APP there's no OTA slot, so `esp.ota` replies
`unsupported` (expected, not a bug).

> **Compile-speed tip:** arduino-cli caches the build per FQBN, and the
> partition scheme is part of the FQBN — alternating schemes between compiles
> throws the sketch cache away each time (~minutes per build). Pick one scheme
> for all your boards, compile once, then `arduino-cli upload -p <port>` per
> board reuses the cached binaries.

## Optional peripherals (compile-time)

Ethernet and camera cost real flash, so they're **off by default**; board pin
maps live on the Python side (`espbridge.eth` / `espbridge.camera` presets), so
one build serves every board. Because Arduino compiles each library source as
its own unit, enable these with a global build flag rather than a sketch
`#define`:

```sh
# arduino-cli
arduino-cli compile --fqbn esp32:esp32:esp32:PartitionScheme=huge_app \
  --build-property "build.extra_flags=-DBRIDGE_ENABLE_ETH=1" Bridge
```

Flags: `BRIDGE_ENABLE_ETH=1` (RMII or SPI W5500), `BRIDGE_ENABLE_CAM=1`
(OV-series + PSRAM on esp32/s2/s3), `BRIDGE_ENABLE_BLE=0` (USB-only build).

**Classic-ESP32 IRAM trade-off:** Wi-Fi + Bluedroid fill the chip's instruction
RAM, so the default classic build ships without SD-card support (LittleFS still
works) and without deep/light sleep. Building with `-DBRIDGE_ENABLE_BLE=0`
frees the BT IRAM and re-enables SD, SDMMC and sleep. S2/S3/C3/C6 have
everything in every build.

## Bluetooth discovery

The board advertises as `espbridge_<mac>` — or `espbridge_<mac>_<name>` once you
assign a name (`espbridge set-name relays`; updates on the next reset).
`espbridge scan --ble` lists every bridge in range.

## Layout

```
library.properties      Library Manager metadata
keywords.txt            IDE syntax highlighting
examples/Bridge/        the one-line example sketch
src/PythonEspBridge.*    public API (EspBridge.begin)
src/espbridge/          bridge core: protocol contract, framing, links, config
src/mod_*.cpp           peripheral/task modules (GPIO, I2C, Wi-Fi, BLE, ...)
src/link_ble.cpp        Bluetooth transport (NUS-style GATT service)
```

Wire protocol: see [`docs/PROTOCOL.md`](https://github.com/HamzaYslmn/python-esp-bridge/blob/main/docs/PROTOCOL.md).
The contract constants in `src/espbridge/commands.h` mirror the Python
package's `espbridge/constants.py`.
