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
#define BRIDGE_PASSWORD "espbridge"  // Bluetooth password ("" = open access)
#define BRIDGE_BLE_LINK 1            // 0 = USB only
#define BRIDGE_WIFI_COEX 1           // classic ESP32: Wi-Fi up before BLE (coex)
```

Change the password here and reflash. USB never asks for a password.
`BRIDGE_WIFI_COEX` pre-starts the Wi-Fi driver before Bluedroid so Wi-Fi /
ESP-NOW / BLE can all run together (required init order on classic ESP32);
set it to 0 to reclaim ~50 KB heap on boards that never use the radio.

## Flashing (Arduino IDE)

1. Select your board, e.g. **ESP32 Dev Module** (classic ESP-32S/ESP-32D,
   30/38-pin DevKits) or **ESP32S3 Dev Module**.
2. **Tools → Partition Scheme → "Huge APP (3MB No OTA/1MB SPIFFS)"** — the
   Wi-Fi + BLE build does not fit the default 1.2 MB app partition.
3. (S3 only) Tools → USB CDC On Boot → **Enabled**.
4. Open `firmware.ino`, Upload.

To build without BLE (smaller, more free heap, no Bluetooth link), add
`#define BRIDGE_ENABLE_BLE 0` at the top of `config.h` — then the default
partition scheme also fits.

## Flashing (arduino-cli)

```sh
arduino-cli core install esp32:esp32
# classic ESP32
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
