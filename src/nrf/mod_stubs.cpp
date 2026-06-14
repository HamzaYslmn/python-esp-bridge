// Unsupported-module stubs for the nRF52 build. Every peripheral the protocol
// defines that this part/firmware does not implement still needs its handler
// symbol to satisfy the dispatcher in protocol.cpp; each replies ST_UNSUPPORTED
// (the host gates on SYS_INFO.caps and never sends these anyway). The pollers
// and wifi_init() are no-ops. The real nRF52 handlers (SYS/GPIO/ADC/PWM/I2C)
// and the BLE link live in their own files.
#if defined(ARDUINO_ARCH_NRF52)
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

// Implemented in their own nRF files: SYS, GPIO, ADC, PWM, I2C, SPI, UART,
// 1-Wire, NVS, FS, BLE link. The rest have no nRF52 hardware → stub here.
UNSUPPORTED_STUB(wifi_handle,    MOD_WIFI)
UNSUPPORTED_STUB(net_handle,     MOD_NET)
UNSUPPORTED_STUB(espnow_handle,  MOD_ESPNOW)
UNSUPPORTED_STUB(rmt_handle,     MOD_RMT)
UNSUPPORTED_STUB(ota_handle,     MOD_OTA)
UNSUPPORTED_STUB(twai_handle,    MOD_TWAI)
UNSUPPORTED_STUB(i2s_handle,     MOD_I2S)
UNSUPPORTED_STUB(eth_handle,     MOD_ETH)
UNSUPPORTED_STUB(cam_handle,     MOD_CAM)
UNSUPPORTED_STUB(mcpwm_handle,   MOD_MCPWM)
UNSUPPORTED_STUB(watch_handle,   MOD_WATCH)

// Pollers (called every loop on rx_task / slow_task) — nothing to service.
// (uart_poll lives in mod_uart.cpp now.)
void wifi_init() {}
void wifi_poll() {}
void net_poll() {}
void twai_poll() {}
void watch_poll() {}

#endif  // ARDUINO_ARCH_NRF52
