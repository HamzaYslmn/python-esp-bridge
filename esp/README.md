# Bridge firmware (flash once)

Arduino sketch for the python-esp-bridge firmware. Flash it once; afterwards every
peripheral is driven live from Python (`pip install python-esp-bridge`).

## Requirements

- **arduino-esp32 core 3.x** (Boards Manager → "esp32 by Espressif Systems").
  The sketch uses the 3.x LEDC/String APIs and will not build on 2.x.
- No external libraries — BLE uses the bundled Bluedroid library.

## Flashing (Arduino IDE)

1. Select your board, e.g. **ESP32 Dev Module** (classic ESP-32S/ESP-32D,
   30/38-pin DevKits) or **ESP32S3 Dev Module**.
2. **Tools → Partition Scheme → "Huge APP (3MB No OTA/1MB SPIFFS)"** — the
   Wi-Fi + BLE build does not fit the default 1.2 MB app partition.
3. (S3 only) Tools → USB CDC On Boot → **Enabled**.
4. Open `esp.ino`, Upload.

To build without BLE (smaller, more free heap), add `#define BRIDGE_ENABLE_BLE 0`
at the top of `config.h` — then the default partition scheme also fits.

## Flashing (arduino-cli)

```sh
arduino-cli core install esp32:esp32
# classic ESP32
arduino-cli compile --fqbn "esp32:esp32:esp32:PartitionScheme=huge_app" esp
arduino-cli upload  --fqbn "esp32:esp32:esp32:PartitionScheme=huge_app" -p COM5 esp
# ESP32-S3
arduino-cli compile --fqbn "esp32:esp32:esp32s3:PartitionScheme=huge_app,CDCOnBoot=cdc" esp
```

## Quick sanity check

After flashing, from Python:

```python
from espbridge import Bridge
with Bridge() as esp:
    print(esp.info)        # chip, MAC, fw version, capabilities
```

Wire protocol: see [`../docs/PROTOCOL.md`](../docs/PROTOCOL.md). The contract
constants live in [`src/espbridge/commands.h`](src/espbridge/commands.h) and
must mirror the Python package's `src/espbridge/constants.py`.

## Sketch layout

```
esp.ino                  entry point (FreeRTOS task startup)
src/espbridge/           bridge core: protocol contract, framing, config
src/mod_*.cpp            peripheral/task modules (GPIO, I2C, Wi-Fi, BLE, ...)
```

Arduino compiles the `src/` folder recursively — open `esp.ino` and upload as
usual; no extra steps.
