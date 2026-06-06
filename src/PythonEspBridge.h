// python esp bridge — flash-once firmware that exposes every ESP32 peripheral
// to the Python `python-esp-bridge` package over USB serial or Bluetooth.
//
//   #include <PythonEspBridge.h>
//   void setup() { EspBridge.begin(); }   // BLE password "espbridge", Bluetooth on
//   void loop()  {}                        // never runs — begin() owns the board
//
// begin() spins up the FreeRTOS task model (TX / RX / network tasks) and does
// NOT return: it deletes the Arduino loop task to hand its stack back to the
// heap (a classic ESP32 running Wi-Fi + Bluedroid needs it). Heavy peripherals
// (Ethernet, camera) and a BLE-free build are compile-time opt-ins — see README.
#pragma once
#include <Arduino.h>

class EspBridgeClass {
 public:
  // password: Bluetooth auth secret a client must present ("" = open; USB never asks).
  // ble:      start the Bluetooth link (ignored on USB-only builds).
  void begin(const char* password = "espbridge", bool ble = true);
};

extern EspBridgeClass EspBridge;
