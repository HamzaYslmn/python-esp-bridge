# Bridge firmware (flash once)

Arduino sketch for the python-esp-bridge firmware. Flash it once; afterwards every
peripheral is driven live from Python (`pip install python-esp-bridge`) — over
USB serial **or Bluetooth**.

## Requirements

- **arduino-esp32 core 3.x** (Boards Manager → "esp32 by Espressif Systems").
  The sketch uses the 3.x LEDC/String APIs and will not build on 2.x.
- No external libraries — BLE uses the bundled Bluedroid library.
  Note: core 3.3.x ships NimBLE (not Bluedroid) on S3/C3/C6, so Bluetooth is
  currently classic-ESP32 only; other chips build USB-only automatically.

## Configuration (top of `firmware.ino`)

```cpp
#define BRIDGE_ENABLE_ETH 0          // opt-in: Ethernet (RMII or SPI W5500)
#define BRIDGE_ENABLE_CAM 0          // opt-in: camera (esp32/s2/s3 + PSRAM)
#define BRIDGE_PASSWORD "espbridge"  // Bluetooth password ("" = open access)
#define BRIDGE_BLE_LINK 1            // 0 = USB only
```

Change the password here and reflash. USB never asks for a password.
Wi-Fi / ESP-NOW / BLE all coexist: the BLE link is up at boot, and the Wi-Fi
driver comes up lazily on the host's first Wi-Fi/ESP-NOW command — so a
BLE-only board never pays the Wi-Fi driver's ~30–50 KB heap (which is what
used to starve I2C/SPI). The SW coex arbiter shares the radio; IDF defaults
are left untouched (never `WIFI_PS_NONE` with BT).
Ethernet and camera stay compile-time opt-ins because they cost real flash;
board pin maps live on the Python side (`espbridge.eth` / `espbridge.camera`
presets), so one firmware build serves every board.

**Classic-ESP32 IRAM trade-off:** Wi-Fi + Bluedroid fill the chip's
instruction RAM. The default classic build therefore ships without SD-card
support (LittleFS works) and without deep/light sleep. Building with
`#define BRIDGE_ENABLE_BLE 0` (USB-only) frees the BT IRAM and re-enables
SD, SDMMC and sleep. S2/S3/C3/C6 have everything in every build.

## Partition scheme (pick one)

The Wi-Fi + BLE build needs a >1.2 MB app slot, so the **default table won't
fit** — choose one of these at the first USB flash:

| Scheme | App | OTA? | Pick when |
|---|---|---|---|
| **Huge APP (3MB No OTA/1MB SPIFFS)** | 3 MB | no | default — biggest app; you always reflash over USB |
| **Minimal SPIFFS (1.9MB APP with OTA)** | 1.9 MB | yes | you want cable-free firmware updates |

OTA (`espbridge.ota`, USB *or* Bluetooth — see
`examples/system/ota_update.py`) updates the firmware binary itself; it's the
cable-free complement to the BLE link for deployed boards. It needs the OTA
partition above — on Huge APP there's no OTA slot, so `esp.ota` replies
`unsupported` (expected, not a bug). Pick Huge APP if you always have USB
access; nothing else depends on the choice.

## Flashing (Arduino IDE)

1. Select your board, e.g. **ESP32 Dev Module** (classic ESP-32S/ESP-32D,
   30/38-pin DevKits) or **ESP32S3 Dev Module**.
2. **Tools → Partition Scheme →** Huge APP, or Minimal SPIFFS for OTA (see
   the table above) — *not* the default, which the Wi-Fi + BLE build overflows.
3. (S3 only) Tools → USB CDC On Boot → **Enabled**.
4. Open `firmware.ino`, Upload.

To build without BLE (smaller, more free heap, no Bluetooth link), add
`#define BRIDGE_ENABLE_BLE 0` at the top of `config.h` — then the default
partition scheme also fits.

## Flashing (arduino-cli)

```sh
arduino-cli core install esp32:esp32
# classic ESP32 (PartitionScheme=huge_app, or min_spiffs for OTA)
arduino-cli compile --fqbn "esp32:esp32:esp32:PartitionScheme=huge_app" firmware
arduino-cli upload  --fqbn "esp32:esp32:esp32:PartitionScheme=huge_app" -p COM5 firmware
# ESP32-S3
arduino-cli compile --fqbn "esp32:esp32:esp32s3:PartitionScheme=huge_app,CDCOnBoot=cdc" firmware
```

## Quick sanity check

After flashing, from Python:

```python
from espbridge import Bridge
with Bridge() as esp:          # USB
    print(esp.info)            # chip, MAC, fw version, capabilities

with Bridge(ble=True) as esp:  # Bluetooth (default password "espbridge")
    print(esp.info)
```

Over Bluetooth the board advertises as `espbridge_<mac>` — or
`espbridge_<mac>_<name>` once you assign a name (`espbridge set-name relays`;
the advertised name updates on the next reset). `espbridge scan --ble` lists
every bridge in range.

Wire protocol: see [`../docs/PROTOCOL.md`](../docs/PROTOCOL.md). The contract
constants live in [`src/espbridge/commands.h`](src/espbridge/commands.h) and
must mirror the Python package's `src/espbridge/constants.py`.

## Sketch layout

```
firmware.ino             entry point (user config + FreeRTOS task startup)
src/espbridge/           bridge core: protocol contract, framing, links, config
src/mod_*.cpp            peripheral/task modules (GPIO, I2C, Wi-Fi, BLE, ...)
src/link_ble.cpp         Bluetooth transport (NUS-style GATT service)
```

Arduino compiles the `src/` folder recursively — open `firmware.ino` and
upload as usual; no extra steps.
