// python-esp-bridge — flash once, control every ESP32 peripheral from Python
// over USB serial or Bluetooth. Protocol spec: docs/PROTOCOL.md in the repo.
//
// Built on FreeRTOS: a TX task owns the serial output, an RX task runs all
// fast peripheral handlers, and a network task owns Wi-Fi/NET/BLE — so a
// blocking TCP/BLE connect never stalls GPIO/I2C/SPI traffic. See protocol.h.
//
// Requires arduino-esp32 core 3.x. With BLE enabled (default) select a
// partition scheme with a large app slot, e.g. "Huge APP (3MB No OTA)".
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

#if BRIDGE_BLE_LINK
  // Bluetooth link: same protocol, no USB cable. Clients authenticate with
  // SYS_AUTH (BRIDGE_PASSWORD above) before any other command is accepted.
  link_ble_init(BRIDGE_PASSWORD);
#endif

  // Boot banner: host waits for this after the DTR/RTS auto-reset.
  uint8_t info[64];
  uint16_t n = sys_build_info(info);
  proto_send_event(SYS_READY, info, n);
}

void loop() {
  // Everything runs in dedicated FreeRTOS tasks; park the Arduino loop task.
  vTaskDelay(portMAX_DELAY);
}
