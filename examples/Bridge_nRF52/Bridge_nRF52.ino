/*
 * Python ESP Bridge — Nordic nRF52840 build (Seeed XIAO nRF52840 / Adafruit
 * Bluefruit boards). Flash once, then drive the board from Python over USB
 * serial or Bluetooth Low Energy with `pip install python-esp-bridge`.
 *
 * This build exposes a capability-gated subset of the bridge:
 *   BLE link, GPIO (+ edge-watch), ADC, PWM, I2C (e.g. SSD1306 OLED), SPI,
 *   UART, 1-Wire, LittleFS filesystem, NVS, deep sleep and BLE scan.
 * Wi-Fi, ESP-NOW, DAC, touch, CAN, I2S, RMT, Ethernet, camera, OTA and the
 * BLE GATT server/client are not available on this part and report as
 * unsupported — the host reads SYS_INFO capabilities and does not offer them.
 *
 * Board setup (Arduino IDE):
 *   1. Boards Manager → install "Seeed nRF52 Boards" (or "Adafruit nRF52").
 *      This is the Bluefruit/FreeRTOS core — NOT the mbed-enabled core.
 *   2. Tools → Board → Seeed XIAO nRF52840 (non-Sense is fine).
 *   3. Select the port and Upload. (Double-tap reset for the UF2 bootloader if
 *      the port doesn't appear.)
 *
 * Example usage from the host PC:
 *   from espbridge import Bridge
 *   with Bridge() as esp:               # USB serial
 *       print(esp.info)                 # chip == NRF52840, capabilities
 *   with Bridge(ble=True) as esp:       # Bluetooth (password "espbridge")
 *       esp.gpio.write(13, 1)
 */
#include <PythonEspBridge.h>

void setup() {
  EspBridge.begin();               // BLE password "espbridge", Bluetooth enabled
  // EspBridge.begin("secret");    // custom Bluetooth password
  // EspBridge.begin("", false);   // USB-only mode (Bluetooth off)
}

void loop() {}                     // intentionally empty — begin() owns the board
