// python-esp-bridge — nRF52840 platform glue (heap, reset cause, link Serial,
// log hook). Counterpart to src/esp/plat_esp.cpp. The Bluefruit core ships
// FreeRTOS (heap_4), so the bridge's task/queue model runs unchanged.
#if defined(ARDUINO_ARCH_NRF52)
#include "espbridge/platform.h"
#include "espbridge/protocol.h"
#include "espbridge/config.h"
#include <InternalFileSystem.h>

// Single, idempotent LittleFS (internal flash) mount shared by the FS, NVS and
// device-name-persistence code. Mounting twice can re-init the volume, so all
// callers funnel through here. Declared extern (no shared header) in the nRF
// files that use it.
bool nrf_internalfs_begin() {
  static bool done = false, ok = false;
  if (!done) { ok = InternalFS.begin(); done = true; }
  return ok;
}

// The Bluefruit core's FreeRTOS uses a malloc-wrapper heap, so the heap_4 query
// xPortGetFreeHeapSize() is not linked. The core's debug helpers report heap
// usage from the newlib arena instead (declared in cores/.../utility/debug.h).
extern int dbgHeapTotal(void);
extern int dbgHeapUsed(void);

uint32_t plat_free_heap() { return (uint32_t)(dbgHeapTotal() - dbgHeapUsed()); }

// Raw RESETREAS captured at boot (before it is cleared) so SYS_WAKE_CAUSE can
// still report a System-OFF GPIO wake after plat_reset_reason() has consumed it.
static uint32_t s_resetreas = 0;
uint32_t plat_resetreas() { return s_resetreas; }

const char* plat_reset_reason() {
  // RESETREAS bits are sticky across resets; read then clear by writing back.
  uint32_t r = NRF_POWER->RESETREAS;
  s_resetreas = r;
  NRF_POWER->RESETREAS = r;
  if (r & POWER_RESETREAS_RESETPIN_Msk) return "pin reset";
  if (r & POWER_RESETREAS_DOG_Msk)      return "watchdog";
  if (r & POWER_RESETREAS_SREQ_Msk)     return "software reset";
  if (r & POWER_RESETREAS_LOCKUP_Msk)   return "cpu lockup";
  if (r & POWER_RESETREAS_OFF_Msk)      return "wake from system off";
  return "power-on";
}

void plat_serial_begin() {
  // Serial is USB CDC (TinyUSB); it buffers internally and ignores the baud
  // argument. No setRxBufferSize/setRxFIFOFull on this core.
  Serial.begin(115200);
}

// No IDF-style logging subsystem and the USB-CDC link is the only serial port,
// so there is nothing to redirect into SYS_LOG events.
void proto_log_hook_install() {}

#endif  // ARDUINO_ARCH_NRF52
