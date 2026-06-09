// python-esp-bridge — frame codec, FreeRTOS task plumbing, dispatch.
//
// Task model (radio stacks run on core 0; bridge tasks pinned to core 1):
//   tx_task   — sole Serial writer; drains the outbound frame queue.
//   rx_task   — decodes frames; runs fast handlers (SYS/GPIO/analog/PWM/
//               I2C/SPI/UART) inline; pumps GPIO edge + UART RX events.
//   net_task  — owns Wi-Fi/NET/BLE state; executes their (possibly blocking)
//               handlers from a request queue and polls sockets/scan results.
// proto_reply*/proto_send_event are therefore safe from ANY task or callback.
#pragma once
#include <Arduino.h>
#include "config.h"

void proto_init();    // create queues (before any proto_* call)
void proto_start();   // spawn tx/rx/net tasks (end of setup())

// Route IDF log output (Wi-Fi/BT stacks) away from UART0 — which IS the
// protocol link — into SYS_LOG events. Install first thing in setup().
void proto_log_hook_install();

// All functions below are thread-safe: they enqueue frames for tx_task to write.
// They may be called from any task or interrupt callback.
void proto_reply(uint8_t seq, uint16_t cmd, const uint8_t* data, uint16_t len);
void proto_reply_ok(uint8_t seq, uint16_t cmd);
void proto_reply_err(uint8_t seq, uint16_t cmd, uint8_t status);
void proto_send_event(uint16_t cmd, const uint8_t* data, uint16_t len);
void proto_log(uint8_t level, const char* msg);
void proto_log_heap(const char* stage);  // sends a SYS_LOG event: "<stage>, N B free heap"

// Block until every queued frame is on the wire (baud switch / reset).
void proto_tx_flush();

uint32_t proto_dropped_events();

uint16_t crc16_ccitt(const uint8_t* data, uint16_t len);

// Big-endian read/write helpers used throughout the protocol layer.
static inline uint16_t rd16(const uint8_t* p) { return ((uint16_t)p[0] << 8) | p[1]; }
static inline uint32_t rd32(const uint8_t* p) {
  return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}
static inline void wr16(uint8_t* p, uint16_t v) { p[0] = v >> 8; p[1] = v; }
static inline void wr32(uint8_t* p, uint32_t v) { p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v; }
