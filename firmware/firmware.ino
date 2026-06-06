// python-esp-bridge — flash once, control every ESP32 peripheral from Python
// over USB serial or Bluetooth. Protocol spec: docs/PROTOCOL.md in the repo.
//
// Built on FreeRTOS: a TX task owns the serial output, an RX task runs all
// fast peripheral handlers, and a network task owns Wi-Fi/NET/BLE — so a
// blocking TCP/BLE connect never stalls GPIO/I2C/SPI traffic. See protocol.h.
//
// Requires arduino-esp32 core 3.x. With BLE enabled (default) select a
// partition scheme with a large app slot — "Huge APP (3MB No OTA)", or
// "Minimal SPIFFS (1.9MB APP with OTA)" if you want over-the-air updates.

// ---- compile-time opt-ins (must precede the config.h include) ---------------
// Heavy peripherals, off by default. Board pin maps live on the Python side
// (espbridge.eth / espbridge.camera presets) — no firmware edits per board.
#ifndef BRIDGE_ENABLE_ETH
#define BRIDGE_ENABLE_ETH 0  // Ethernet PHY (RMII e.g. WT32-ETH01, or SPI W5500)
#endif
#ifndef BRIDGE_ENABLE_CAM
#define BRIDGE_ENABLE_CAM 0  // OV-series camera (esp32/s2/s3 + PSRAM only)
#endif

#include "src/espbridge/config.h"
#include "src/espbridge/protocol.h"
#include "src/espbridge/modules.h"
#include "src/espbridge/link.h"

// ---- user configuration -----------------------------------------------------
// Password Bluetooth clients must present before any command is accepted
// (USB never needs it). Change it here and reflash. "" = open access.
#define BRIDGE_PASSWORD "espbridge"

// Set to 0 to turn the Bluetooth link off entirely (USB only).
#define BRIDGE_BLE_LINK 1

void setup() {
  // Route IDF Wi-Fi/BT logs into SYS_LOG events; raw log bytes on UART0 would
  // corrupt protocol frames. Must precede any radio bring-up.
  proto_log_hook_install();

#if !BRIDGE_NATIVE_USB
  Serial.setRxBufferSize(SERIAL_RX_BUF);   // must precede begin(); default 256 is too small
  Serial.setTxBufferSize(SERIAL_TX_BUF);
  Serial.begin(115200);
#else
  Serial.setRxBufferSize(SERIAL_RX_BUF);
  Serial.begin();                          // native USB CDC, baud ignored
#endif

  proto_init();
  gpio_init();
  wifi_init();
  proto_start();   // spawn bridge_tx / bridge_rx / bridge_net tasks

  // Wi-Fi/ESP-NOW stay off until the host's first radio command, then come up
  // lazily (WiFi.mode in the wifi/espnow handlers). A BLE-only board never
  // pays the Wi-Fi driver's heap — which is what kept I2C/SPI from wedging.
  // The SW coex arbiter shares the radio with BLE; leave its defaults alone
  // (never WIFI_PS_NONE with BT). See mod_wifi.cpp / mod_espnow.cpp.
#if BRIDGE_BLE_LINK
  // Bluetooth link: same protocol, no USB cable. Clients authenticate with
  // SYS_AUTH (BRIDGE_PASSWORD above) before any other command is accepted.
  link_ble_init(BRIDGE_PASSWORD);
#endif

  // Boot banner: host waits for this after the DTR/RTS auto-reset.
  uint8_t info[64];
  uint16_t n = sys_build_info(info);
  proto_send_event(SYS_READY, info, n);

  // Everything runs in dedicated FreeRTOS tasks — delete the Arduino loop
  // task instead of parking it: its 8 KB stack goes back to the heap, which
  // a classic ESP32 running Wi-Fi + Bluedroid needs badly (Bluedroid stops
  // delivering notifications below ~8 KB free, killing the BLE link).
  vTaskDelete(nullptr);  // never returns
}

void loop() {}  // never runs (loopTask deleted at end of setup)
