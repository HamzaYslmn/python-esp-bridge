// python-esp-bridge — module entry points wired up by protocol.cpp.
#pragma once
#include <Arduino.h>

// handle(op, seq, payload, len) — fast handlers run on rx_task;
// wifi/net/ble handlers run on net_task (may block).
void sys_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void gpio_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void analog_handle(uint8_t mod, uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);  // ADC+DAC+TOUCH
void pwm_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void i2c_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void spi_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void uart_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void wifi_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void net_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);
void ble_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len);

void gpio_init();
void wifi_init();

// Pollers: gpio/uart run on rx_task, wifi/net on net_task; must not block.
void gpio_poll();
void uart_poll();
void wifi_poll();
void net_poll();

bool wifi_is_active();  // used by ADC2-conflict guard

// SYS_INFO payload builder (also used for the SYS_READY boot banner).
uint16_t sys_build_info(uint8_t* out);

// NVS-persisted device name ("" when unset); appended to the BLE adv name.
const char* sys_device_name();
