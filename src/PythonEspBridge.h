// python-esp-bridge — flash-once firmware that exposes every ESP32 peripheral
// to the Python `python-esp-bridge` package over USB serial or Bluetooth.
//
// Minimal sketch:
//   #include <PythonEspBridge.h>
//   void setup() { EspBridge.begin(); }  // BLE password "espbridge", BLE enabled
//   void loop()  {}                       // never called — begin() does not return
//
// begin() starts the FreeRTOS bridge tasks (TX / RX / network) and then deletes
// the Arduino loop task so its 8 KB stack is returned to the heap. This is
// required on a classic ESP32 running Wi-Fi + Bluedroid: free heap that low
// causes Bluedroid to stop delivering notifications and break the BLE link.
//
// Optional features (heavy peripherals such as Ethernet or camera, and
// BLE-free builds) are compile-time opt-ins — see the README for flags.
#pragma once
#include <Arduino.h>

class EspBridgeClass {
 public:
  // password  — Bluetooth authentication secret the host must present before
  //             commands are accepted. Pass "" for an open (unauthenticated)
  //             link. USB serial never requires authentication.
  // ble       — set false to leave Bluetooth off entirely (saves heap).
  //             Ignored on builds compiled without BRIDGE_BLE.
  void begin(const char* password = "espbridge", bool ble = true);
};

extern EspBridgeClass EspBridge;
