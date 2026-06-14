// python-esp-bridge — platform shim.
//
// The protocol core (COBS framing, CRC, the FreeRTOS task model, SYS info) is
// hardware-independent. The few facts it needs that differ per vendor SDK —
// free heap, the last-reset cause, and how the host link's Serial port is
// brought up — are declared here and implemented once per architecture
// (src/esp/plat_esp.cpp, src/nrf/plat_nrf.cpp). Peripheral handlers live in
// their own per-arch trees and call the Arduino / vendor API directly; this is
// deliberately not a full HAL.
#pragma once
#include <Arduino.h>

uint32_t    plat_free_heap();     // current free heap in bytes
const char* plat_reset_reason();  // human-readable last-reset cause (logged at boot)
void        plat_serial_begin();  // bring up the host-link Serial port (USB CDC / UART)
