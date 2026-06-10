#include "PythonEspBridge.h"
#include "espbridge/config.h"
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/link.h"
#include <esp_system.h>

EspBridgeClass EspBridge;

// Why the chip last reset — logged at boot so a stall that resets the board is
// diagnosable on reconnect (brownout => power; task watchdog => a task starved;
// panic => firmware bug) instead of a mystery.
static const char* reset_reason_str() {
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

void EspBridgeClass::begin(const char* password, bool ble, bool exclusive) {
  // Redirect all IDF Wi-Fi and Bluetooth log output to SYS_LOG protocol events
  // instead of writing raw bytes to UART0. Raw IDF log bytes on UART0 would
  // be interpreted as protocol frame data and corrupt every frame they land in.
  // This hook must be installed before any radio is brought up.
  proto_log_hook_install();

#if !BRIDGE_NATIVE_USB
  Serial.setRxBufferSize(SERIAL_RX_BUF);   // must be called before Serial.begin(); the default 256-byte buffer is far too small for protocol frames
  Serial.setTxBufferSize(SERIAL_TX_BUF);
  Serial.begin(115200);
  // The RX interrupt defaults to firing at 120 of the 128 hardware FIFO
  // bytes — at 1.5 Mbaud that is ~53 µs of interrupt-latency margin, and a
  // single spike under concurrent load overruns the FIFO and corrupts a
  // host->board frame (measured: ~1e-3/op at 12-thread mixed load, zero at
  // 921600). Fire at 64 bytes instead: identical throughput, 8x the margin.
  Serial.setRxFIFOFull(64);
#else
  Serial.setRxBufferSize(SERIAL_RX_BUF);
  Serial.begin();                          // native USB CDC — baud rate argument is ignored by the USB driver
#endif

  proto_init();
  gpio_init();
  wifi_init();
  proto_start();   // spawn the bridge_tx, bridge_rx, and bridge_slow FreeRTOS tasks

  { char msg[64];
    snprintf(msg, sizeof(msg), "boot: last reset = %s", reset_reason_str());
    proto_log(1, msg); }

  // Wi-Fi and ESP-NOW stay off until the host sends its first radio command
  // (lazy init). A board that only uses BLE therefore never loads the Wi-Fi
  // driver and never pays the associated heap cost. When Wi-Fi does come up,
  // the IDF software coex arbiter shares the radio with BLE automatically —
  // its defaults are correct; leave them alone.
#if BRIDGE_BLE
  if (ble) link_ble_init(password);        // SYS_AUTH gate uses `password`
#else
  (void)password;
  (void)ble;
#endif

  // Send the boot banner. After a DTR/RTS auto-reset the host waits for this
  // SYS_READY event before issuing any commands.
  uint8_t info[64];
  uint16_t n = sys_build_info(info);
  proto_send_event(SYS_READY, info, n);

  // The bridge runs entirely in its own FreeRTOS tasks (tx, rx, net).
  // Default: delete the Arduino loop task to reclaim its 8 KB stack for the
  // heap. This is not just tidiness: Bluedroid stops delivering notifications
  // when free heap drops below roughly 8 KB, which silently kills the BLE
  // link. With exclusive=false the sketch keeps loop() for its own work.
  if (exclusive) vTaskDelete(nullptr);  // never returns
}
