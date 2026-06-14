// python-esp-bridge — ESP32 platform glue (heap, reset cause, link Serial,
// and the IDF log redirect). Pulled out of the shared protocol core so that
// core stays architecture-independent.
#if defined(ARDUINO_ARCH_ESP32)
#include "espbridge/platform.h"
#include "espbridge/protocol.h"
#include "espbridge/config.h"
#include <esp_system.h>
#include <esp_log.h>

uint32_t plat_free_heap() { return ESP.getFreeHeap(); }

const char* plat_reset_reason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:   return "power-on";
    case ESP_RST_EXT:       return "external reset";
    case ESP_RST_SW:        return "software reset";
    case ESP_RST_PANIC:     return "panic/exception";
    case ESP_RST_INT_WDT:   return "interrupt watchdog";
    case ESP_RST_TASK_WDT:  return "task watchdog (a task starved the CPU)";
    case ESP_RST_WDT:       return "watchdog";
    case ESP_RST_DEEPSLEEP: return "deep-sleep wake";
    case ESP_RST_BROWNOUT:  return "brownout (supply voltage dropped)";
    case ESP_RST_SDIO:      return "SDIO";
    default:                return "unknown";
  }
}

void plat_serial_begin() {
#if !BRIDGE_NATIVE_USB
  Serial.setRxBufferSize(SERIAL_RX_BUF);   // must precede Serial.begin(); the default 256-byte buffer is far too small for protocol frames
  Serial.setTxBufferSize(SERIAL_TX_BUF);
  Serial.begin(115200);
  // RX interrupt defaults to 120 of 128 FIFO bytes — ~53 µs margin at 1.5 Mbaud,
  // which a load spike can overrun and corrupt a frame. Fire at 64: same
  // throughput, 8x the margin.
  Serial.setRxFIFOFull(64);
#else
  Serial.setRxBufferSize(SERIAL_RX_BUF);
  Serial.begin();                          // native USB CDC — baud argument ignored by the USB driver
#endif
}

// ---- IDF log capture ---------------------------------------------------------
// Wi-Fi/BT stacks log via esp_log to UART0 — the COBS frame port — so raw bytes
// would corrupt frames. This hook redirects IDF log output into SYS_LOG events.
// (ROM boot and crash/panic text still hit UART0 raw; uninterceptable here.)
static int bridge_vprintf(const char* fmt, va_list ap) {
#if BRIDGE_NATIVE_USB
  return vprintf(fmt, ap);  // UART0 is free on native-USB chips: keep IDF logs
#else
  char line[160];
  int n = vsnprintf(line, sizeof(line), fmt, ap);
  // esp_log appends CR/LF; strip it since SYS_LOG delivers one line at a time.
  size_t L = n < 0 ? 0 : (n < (int)sizeof(line) ? (size_t)n : sizeof(line) - 1);
  while (L && (line[L - 1] == '\n' || line[L - 1] == '\r')) line[--L] = 0;
  if (L) proto_log(1, line);  // no-op before proto_init() (tx queue guard)
  return n;
#endif
}

void proto_log_hook_install() {
  esp_log_set_vprintf(bridge_vprintf);
}

#endif  // ARDUINO_ARCH_ESP32
