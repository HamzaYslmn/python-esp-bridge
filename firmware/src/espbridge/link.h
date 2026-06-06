// python-esp-bridge — wireless link layer: the same COBS frame stream the USB
// serial port carries, over a Nordic-UART-style BLE GATT service.
//
//   service 6e400001-b5a3-f393-e0a9-e50e24dcca9e
//     RX  6e400002-... host -> board (write / write-no-response)
//     TX  6e400003-... board -> host (notify, chunked to the ATT MTU)
//
// BLE clients must authenticate with SYS_AUTH (payload = password) before any
// other command is accepted; protocol.cpp enforces this per connection.
// MUST stay in sync with the Python package: src/espbridge/constants.py.
#pragma once
#include <Arduino.h>
#include "config.h"

// Frame origins / destinations (protocol.cpp routing).
#define LINK_USB 0
#define LINK_BLE 1

#define BLE_LINK_SERVICE_UUID "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define BLE_LINK_RX_UUID      "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
#define BLE_LINK_TX_UUID      "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

// Register the GATT service and start advertising. Call from setup() after
// proto_start(). `password` is what BLE clients must present via SYS_AUTH
// (define BRIDGE_PASSWORD at the top of firmware.ino).
// No-op on chips without BLE or when BRIDGE_ENABLE_BLE is 0.
void link_ble_init(const char* password);

bool link_ble_enabled();    // link_ble_init() succeeded
bool link_ble_connected();  // a BLE central is currently connected
bool link_ble_authed();     // ...and presented the correct password
void link_ble_set_authed(bool v);
const char* link_ble_password();

// TX: notify the connected client, chunked to MTU. tx_task only.
void link_ble_write(const uint8_t* data, uint16_t len);

// RX: drain bytes written by the BLE client into buf; returns bytes copied.
uint16_t link_ble_read(uint8_t* buf, uint16_t maxlen);

// The GATT server the link created (BLEServer*), so mod_ble can share it
// instead of creating a second one. nullptr when the link is disabled.
void* link_ble_server();
