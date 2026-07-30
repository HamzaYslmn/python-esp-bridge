// python-esp-bridge — wireless link layer. BLE and TCP carry the exact same COBS
// frame stream as the USB serial port; only the pipe differs. Both require
// SYS_AUTH before any other command (protocol.cpp enforces it per connection).
// MUST stay in sync with the Python package: src/espbridge/constants.py.
#pragma once
#include <Arduino.h>
#include "config.h"

// Link origin / destination, used by protocol.cpp for routing frames.
#define LINK_USB 0
#define LINK_BLE 1
#define LINK_TCP 2

// Nordic-UART-style service: RX = host -> board (write), TX = board -> host
// (notify, chunked to the ATT MTU).
#define BLE_LINK_SERVICE_UUID "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define BLE_LINK_RX_UUID      "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
#define BLE_LINK_TX_UUID      "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

// Register the GATT service and start advertising. No-op on chips without BLE.
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

// TX (tx_task only) — non-blocking. tx_task sends one MTU-sized chunk per pass
// while writable, then moves on to the other link.
bool link_ble_up();        // a central is connected (frames may be in flight)
bool link_ble_power(bool battery);  // conn-params profile; false if no central
bool link_ble_writable();  // ...and the BT stack can take a notification now
uint16_t link_ble_write_chunk(const uint8_t* data, uint16_t len);  // bytes consumed

// RX (rx_task only): drain bytes written by the BLE client; returns bytes copied.
uint16_t link_ble_read(uint8_t* buf, uint16_t maxlen);

// The link layer's BLEServer*, so mod_ble.cpp reuses it instead of creating a
// second GATT server. nullptr when BLE is disabled or uninitialised.
void* link_ble_server();

// SYS_RADIO_OFF: shut the BT stack down until reboot (false while a central is
// connected), plus the sticky flag that stops mod_ble re-initing it.
bool link_ble_shutdown();
bool link_bt_dead();

// ---- Wi-Fi (TCP) link -------------------------------------------------------
// server = nullptr/"" -> listen on `port`; otherwise dial "host" or "host:port"
// (dial-home is the mode for hundreds of boards — see link_tcp.cpp).
// ssid = nullptr/"" -> use the credentials stored in NVS by WIFI_LINK_SETUP.
// Returns false if the link could not be armed (no credentials, no heap, or
// ESP-NOW already owns the radio); the board keeps running without it.
bool link_tcp_begin(const char* ssid, const char* pass, const char* server,
                    uint16_t port, const char* password);
void link_tcp_stop();
// WIFI_LINK_SETUP: store the config (optionally in NVS) without starting it.
bool link_tcp_configure(const char* ssid, const char* pass, const char* server,
                        uint16_t port, bool persist);
void link_tcp_forget();  // erase the stored config
bool link_tcp_enabled();   // armed (joining, listening, dialing or connected)
bool link_tcp_up();        // a peer socket is open
bool link_tcp_authed();    // ...and it presented the correct password
void link_tcp_set_authed(bool v);
uint32_t link_tcp_tx_errors();  // sends that failed for a non-transient reason
const char* link_tcp_password();

// WIFI_LINK_STATUS: 0 off | 1 joining | 2 listening/dialing | 3 peer connected.
uint8_t link_tcp_state();
uint32_t link_tcp_ip();
uint16_t link_tcp_port();

void link_tcp_poll();  // accept / dial / backoff, on slow_task
uint16_t link_tcp_write_chunk(const uint8_t* data, uint16_t len);
uint16_t link_tcp_read(uint8_t* buf, uint16_t maxlen);

// Auth gate over both wireless links: USB implies physical access and is always
// trusted; BLE and TCP must present the password via SYS_AUTH first.
bool link_needs_auth(uint8_t link);
bool link_authed(uint8_t link);
void link_set_authed(uint8_t link, bool v);
const char* link_auth_password(uint8_t link);
