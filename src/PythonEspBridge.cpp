#include "PythonEspBridge.h"
#include "espbridge/config.h"
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/link.h"
#include "espbridge/platform.h"

EspBridgeClass EspBridge;

void EspBridgeClass::begin(const char* password, bool ble, bool exclusive) {
  // Redirect vendor radio/log output away from the frame port into SYS_LOG
  // events (ESP only; a no-op on nRF). Must be installed before any radio.
  proto_log_hook_install();

  // Bring up the host-link Serial port (UART or native USB CDC, per arch).
  plat_serial_begin();

  proto_init();
  gpio_init();
  wifi_init();
  proto_start();   // spawn the bridge_tx, bridge_rx, and bridge_slow FreeRTOS tasks

  // Why the chip last reset — logged at boot so a stall that resets the board is
  // diagnosable on reconnect (brownout/watchdog/panic) instead of a mystery.
  { char msg[64];
    snprintf(msg, sizeof(msg), "boot: last reset = %s", plat_reset_reason());
    proto_log(1, msg); }

  // Wi-Fi/ESP-NOW stay off until the host's first radio command (lazy init), so a
  // BLE-only board never pays the Wi-Fi driver's heap cost. Coex defaults are
  // correct once it does come up — leave them alone.
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

  // The bridge runs in its own tasks. Default: delete the Arduino loop task to
  // reclaim its 8 KB stack — not just tidiness, since Bluedroid stops delivering
  // notifications below ~8 KB free. exclusive=false keeps loop() for the sketch.
  if (exclusive) vTaskDelete(nullptr);  // never returns
}
