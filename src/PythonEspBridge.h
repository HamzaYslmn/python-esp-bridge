// python-esp-bridge — flash-once firmware that exposes every ESP32 peripheral
// to the Python `python-esp-bridge` package over USB serial or Bluetooth.
//
// Minimal sketch:
//   #include <PythonEspBridge.h>
//   void setup() { EspBridge.begin(); }  // BLE password "espbridge", BLE enabled
//   void loop()  {}                       // never called — begin() does not return
//
// begin() starts the FreeRTOS bridge tasks and spreads them across both cores
// of a dual-core ESP32 (see the task-layout note in espbridge/config.h):
// TX and the blocking handlers share core 0 with the radio stacks they call
// into; RX + the bus-touching fast handlers own core 1 — so replies transmit
// while the next command executes, and nothing timing-sensitive runs next to
// radio interrupts. It then deletes the Arduino loop task so its 8 KB stack
// is returned to the heap. This is required on a classic ESP32 running
// Wi-Fi + Bluedroid: free heap that low causes Bluedroid to stop delivering
// notifications and break the BLE link.
//
// To run your own code alongside the bridge, pass exclusive=false:
//   void setup() { EspBridge.begin("espbridge", true, /*exclusive=*/false); }
//   void loop()  { /* yours — runs on core 1 next to the command handlers */ }
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
  // exclusive — true (default): begin() never returns; the loop task is
  //             deleted and its 8 KB stack goes back to the heap (the safe
  //             choice with BLE + Wi-Fi on a classic ESP32). false: begin()
  //             returns and loop() keeps running, so the sketch can do its
  //             own work next to the bridge — at the cost of that 8 KB.
  void begin(const char* password = "espbridge", bool ble = true,
             bool exclusive = true);
};

extern EspBridgeClass EspBridge;
