/*
 * python esp bridge — flash once, then drive every ESP32 peripheral from
 * Python (pip install python-esp-bridge) over USB serial or Bluetooth.
 *
 * Pick a large-app partition scheme (Tools > Partition Scheme), because the
 * Wi-Fi + BLE build overflows the default 1.2 MB table:
 *   "Huge APP (3MB No OTA)"                 — default
 *   "Minimal SPIFFS (1.9MB APP with OTA)"   — if you want OTA updates
 *
 * Then from the host:
 *   from espbridge import Bridge
 *   with Bridge() as esp:          # USB
 *       print(esp.info)
 *   with Bridge(ble=True) as esp:  # Bluetooth (default password "espbridge")
 *       print(esp.info)
 */
#include <PythonEspBridge.h>

void setup() {
  EspBridge.begin();               // BLE password "espbridge", Bluetooth on
  // EspBridge.begin("secret");    // custom Bluetooth password
  // EspBridge.begin("", false);   // USB only — no Bluetooth link
}

void loop() {}                     // never runs — begin() owns the board
