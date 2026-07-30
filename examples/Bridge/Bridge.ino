/*
 * Python ESP Bridge — flash this sketch once, then control every ESP32
 * peripheral from Python (pip install python-esp-bridge) over USB serial,
 * Bluetooth or Wi-Fi.
 *
 * IMPORTANT: The Wi-Fi + BLE build is too large for the default 1.2 MB
 * partition table.  Before flashing, go to Tools > Partition Scheme and pick:
 *   "Huge APP (3MB No OTA)"               — recommended default
 *   "Minimal SPIFFS (1.9MB APP with OTA)" — if you need OTA firmware updates
 *
 * Example usage from the host PC:
 *   from espbridge import Bridge
 *   with Bridge() as esp:                       # Bluetooth, else USB serial
 *       print(esp.info)
 *   with Bridge("relays") as esp:               # one board, by name or MAC
 *       print(esp.info)
 *   with Bridge(port="COM7") as esp:            # a specific serial port
 *       print(esp.info)
 *   with Bridge(host="192.168.1.50") as esp:    # over Wi-Fi (see wifi.begin below)
 *       print(esp.info)
 */
#include <PythonEspBridge.h>

void setup() {
  EspBridge.usb.begin();   // USB serial — the bridge core starts with this
  EspBridge.ble.begin();   // Bluetooth, password "espbridge"
  // EspBridge.ble.begin("secret");   // ...or a password of your own ("" = open)

  // Wi-Fi is opt-in — it costs ~52 KB of heap, and with BLE also up a classic
  // ESP32 is left with only ~10 KB, so drop ble.begin() on a Wi-Fi board.
  //   EspBridge.wifi.begin("ssid", "pass");                  // listens on 3232
  //   EspBridge.wifi.begin("ssid", "pass", "10.0.0.5");      // dials your host
  //   EspBridge.wifi.begin();                                // stored credentials
  // Or provision from the host instead, keeping the password out of this file:
  //   esp.wifi.link_setup("ssid", "pass")

  EspBridge.run();         // hands this task to the bridge; frees the loop task's 8 KB
}

void loop() {}             // never runs — remove the run() call above to use it
