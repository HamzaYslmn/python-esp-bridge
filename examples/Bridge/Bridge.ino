/*
 * Python ESP Bridge — flash this sketch once, then control every ESP32
 * peripheral from Python (pip install python-esp-bridge) over USB serial
 * or Bluetooth Low Energy.
 *
 * IMPORTANT: The Wi-Fi + BLE build is too large for the default 1.2 MB
 * partition table.  Before flashing, go to Tools > Partition Scheme and pick:
 *   "Huge APP (3MB No OTA)"               — recommended default
 *   "Minimal SPIFFS (1.9MB APP with OTA)" — if you need OTA firmware updates
 *
 * Example usage from the host PC:
 *   from espbridge import Bridge
 *   with Bridge() as esp:               # connect over USB serial
 *       print(esp.info)
 *   with Bridge(ble=True) as esp:       # connect over Bluetooth (password: "espbridge")
 *       print(esp.info)
 */
#include <PythonEspBridge.h>

void setup() {
  EspBridge.begin();               // start with default BLE password "espbridge" and Bluetooth enabled
  // EspBridge.begin("secret");    // use a custom Bluetooth password instead
  // EspBridge.begin("", false);   // USB-only mode — disables Bluetooth entirely
}

void loop() {}                     // intentionally empty — begin() blocks forever and owns the board
