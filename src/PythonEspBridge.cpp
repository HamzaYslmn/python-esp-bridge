#include "PythonEspBridge.h"
#include "espbridge/config.h"
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/link.h"

EspBridgeClass EspBridge;

void EspBridgeClass::begin(const char* password, bool ble) {
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
  // lazily — so a BLE-only board never pays the Wi-Fi driver's heap. The SW
  // coex arbiter shares the radio with BLE; leave its defaults alone.
#if BRIDGE_BLE
  if (ble) link_ble_init(password);        // SYS_AUTH gate uses `password`
#else
  (void)password;
  (void)ble;
#endif

  // Boot banner: the host waits for this after the DTR/RTS auto-reset.
  uint8_t info[64];
  uint16_t n = sys_build_info(info);
  proto_send_event(SYS_READY, info, n);

  // Everything runs in dedicated FreeRTOS tasks — delete the Arduino loop task
  // to return its 8 KB stack to the heap (Bluedroid stops delivering
  // notifications below ~8 KB free, which kills the BLE link).
  vTaskDelete(nullptr);  // never returns
}
