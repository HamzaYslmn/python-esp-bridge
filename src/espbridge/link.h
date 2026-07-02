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

// Link origin / destination constants used by protocol.cpp for routing frames.
#define LINK_USB 0
#define LINK_BLE 1

#define BLE_LINK_SERVICE_UUID "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define BLE_LINK_RX_UUID      "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
#define BLE_LINK_TX_UUID      "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

// Register the GATT service and start advertising. Call from setup() after
// proto_start(). `password` is what BLE clients must present via SYS_AUTH
// (passed in from EspBridge.begin()).
// No-op on chips without BLE or when BRIDGE_ENABLE_BLE is 0.
void link_ble_init(const char* password);

// Classic ESP32 only: releases Classic BT memory back to the heap and starts
// the controller in BLE-only mode. MUST be called before any other BT
// initialization — Wi-Fi coexistence plus dual-mode Bluedroid do not fit in
// RAM together, and a failed Bluedroid init causes a crash on core 0.
// Safe to call repeatedly; no-op on chips other than classic ESP32.
void bt_prepare_ble_only();

bool link_ble_enabled();    // link_ble_init() succeeded
bool link_ble_authed();     // a central is connected and presented the correct password
uint32_t link_ble_rx_dropped();  // bytes lost to RX buffer overflow (diagnostics)
uint32_t link_serial_rx_errors();  // UART overflow/framing error events (0 on native USB)
void link_ble_set_authed(bool v);
const char* link_ble_password();

// TX (tx_task only) — non-blocking chunk interface. tx_task owns the pacing:
// it sends one MTU-sized chunk per pass while link_ble_writable() and moves
// on to the other link the moment this one is congested or down.
bool link_ble_up();        // a central is connected (frames may be in flight)
bool link_ble_power(bool battery);  // conn-params profile; false when no central is connected
bool link_ble_writable();  // ...and the BT stack can take a notification now
// Send at most one MTU-sized notification from data; returns bytes consumed
// (0 when not writable).
uint16_t link_ble_write_chunk(const uint8_t* data, uint16_t len);

// RX: drain bytes written by the BLE client into buf; returns bytes copied.
uint16_t link_ble_read(uint8_t* buf, uint16_t maxlen);

// Returns the BLEServer* created by the link layer, so mod_ble.cpp can
// reuse it rather than creating a second GATT server. Returns nullptr
// when BLE is disabled or link_ble_init() has not been called.
void* link_ble_server();

// SYS_RADIO_OFF support: shut the whole BT stack down until reboot (returns
// false while a central is connected), and the sticky flag that keeps
// mod_ble from re-initing the dead stack afterwards.
bool link_ble_shutdown();
bool link_bt_dead();
