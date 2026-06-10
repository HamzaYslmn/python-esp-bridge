// python-esp-bridge — module entry points wired up by protocol.cpp.
#pragma once
#include <Arduino.h>

// All handlers share the same signature: handle(op, seq, payload, len).
// Fast handlers (GPIO, I2C, SPI, etc.) run inline on rx_task.
// Slow or stateful handlers (Wi-Fi, NET, BLE, etc.) run on slow_task, where
// they may block for extended periods without stalling frame reception.
void sys_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void gpio_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void analog_handle(uint8_t mod, uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);  // ADC+DAC+TOUCH
void pwm_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void i2c_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void spi_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void uart_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void wifi_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void net_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void espnow_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void ble_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void rmt_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void onewire_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void fs_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void nvs_handle_cmd(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void ota_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void twai_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void twai_poll();  // slow_task: driver RX queue -> TWAI_RX_EVT
void i2s_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void eth_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void cam_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void mcpwm_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void watch_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void watch_poll();  // slow_task: evaluate polled watch rules -> WATCH_EVT

// Generates a handler body that returns ST_UNSUPPORTED for every op.
// Use in the #else branch of a chip-capability guard so that all declared
// handler symbols still link. Example:
//   UNSUPPORTED_STUB(i2s_handle, MOD_I2S)
#define UNSUPPORTED_STUB(handler, mod) \
  void handler(uint8_t op, uint8_t seq, const uint8_t*, uint16_t) { \
    proto_reply_err(seq, CMD(mod, op), ST_UNSUPPORTED); \
  }

void gpio_init();
void wifi_init();

// Pollers are called in a tight loop and must never block.
// gpio_poll/uart_poll run on rx_task; wifi_poll/net_poll run on slow_task.
void gpio_poll();
void uart_poll();
void wifi_poll();
void net_poll();

// Wi-Fi radio ownership (heap guards, power-off when unused): see radio.h.

// True if `pin` is an ADC2 channel — ADC2 shares hardware with the Wi-Fi radio,
// so reads on these pins fail/return garbage while Wi-Fi is active. Shared by the
// analog and watch modules (both sample the ADC).
static inline bool pin_is_adc2(uint8_t pin) {
#if defined(CONFIG_IDF_TARGET_ESP32)
  switch (pin) {
    case 0: case 2: case 4: case 12: case 13: case 14: case 15:
    case 25: case 26: case 27: return true;
    default: return false;
  }
#elif defined(CONFIG_IDF_TARGET_ESP32S2) || defined(CONFIG_IDF_TARGET_ESP32S3)
  return pin >= 11 && pin <= 20;
#else
  return false;  // C3/C6: ADC2 unusable/absent; analogRead maps to ADC1
#endif
}
// Fills out[] with the SYS_INFO payload and returns the byte count written.
// Used for both the SYS_INFO response and the SYS_READY boot-banner event.
uint16_t sys_build_info(uint8_t* out);

// NVS-persisted device name ("" when unset); appended to the BLE adv name.
const char* sys_device_name();
