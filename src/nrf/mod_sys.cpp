// SYS (nRF52): ping, info, reset, heap stats, device name, deep sleep.
// Counterpart to src/esp/mod_sys.cpp. This build has no CPU-freq scaling or
// Wi-Fi; CPU_FREQ and light sleep report ST_UNSUPPORTED.
#if defined(ARDUINO_ARCH_NRF52)
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/link.h"
#include "espbridge/platform.h"
#include <InternalFileSystem.h>
#include <nrf_soc.h>
#include <nrf_gpio.h>

using namespace Adafruit_LittleFS_Namespace;

bool nrf_internalfs_begin();       // plat_nrf.cpp
uint32_t plat_resetreas();         // plat_nrf.cpp — raw RESETREAS captured at boot
extern const uint32_t g_ADigitalPinMap[];  // Arduino pin -> nRF GPIO number

// Device name persisted to LittleFS (/bridge_name), the counterpart to the ESP
// NVS-backed name. Loaded lazily on first use.
static char bridge_name[BRIDGE_NAME_MAX + 1];
static bool name_loaded = false;
#define NAME_PATH "/bridge_name"

static void load_device_name() {
  if (name_loaded) return;
  name_loaded = true;
  if (!nrf_internalfs_begin() || !InternalFS.exists(NAME_PATH)) return;
  File f = InternalFS.open(NAME_PATH, FILE_O_READ);
  if (f) {
    int n = f.read((uint8_t*)bridge_name, BRIDGE_NAME_MAX);
    bridge_name[n > 0 ? n : 0] = 0;
    f.close();
  }
}

const char* sys_device_name() { load_device_name(); return bridge_name; }

// 48-bit factory device address from FICR — unique per die, used as the MAC.
static void nrf_read_mac(uint8_t mac[6]) {
  uint32_t lo = NRF_FICR->DEVICEADDR[0];
  uint32_t hi = NRF_FICR->DEVICEADDR[1];
  mac[0] = (uint8_t)(hi >> 8); mac[1] = (uint8_t)hi;
  mac[2] = (uint8_t)(lo >> 24); mac[3] = (uint8_t)(lo >> 16);
  mac[4] = (uint8_t)(lo >> 8);  mac[5] = (uint8_t)lo;
}

uint16_t sys_build_info(uint8_t* out) {
  uint8_t* p = out;
  *p++ = PROTOCOL_VERSION;
  *p++ = FW_VERSION_MAJOR;
  *p++ = FW_VERSION_MINOR;
  *p++ = FW_VERSION_PATCH;
  *p++ = BRIDGE_CHIP;
  *p++ = 0;  // chip revision: not reported on this part

  uint8_t mac[6] = {0};
  nrf_read_mac(mac);
  memcpy(p, mac, 6); p += 6;

  // No Wi-Fi / DAC / touch / ESP-NOW / RMT / CAN / I2S on nRF52. CAPIF folds at
  // compile time — see commands.h.
  uint32_t caps =
        CAPIF(BRIDGE_HAS_BLE,     CAP_BLE)
      | CAPIF(BRIDGE_BLE,         CAP_BLE_FW)
      | CAPIF(BRIDGE_NATIVE_USB,  CAP_NATIVE_USB)
      | CAPIF(BRIDGE_HAS_ONEWIRE, CAP_ONEWIRE)
      | CAPIF(BRIDGE_HAS_FS,      CAP_FS)
      | CAPIF(BRIDGE_HAS_NVS,     CAP_NVS)
      | CAPIF(BRIDGE_HAS_SLEEP,   CAP_SLEEP);
  if (link_ble_enabled()) caps |= CAP_BLE_LINK;
  write_be32(p, caps); p += 4;

#ifdef PINS_COUNT
  *p++ = (uint8_t)PINS_COUNT;
#else
  *p++ = 48;  // nRF52840: P0.00..P1.15
#endif
  *p++ = 1;   // 1 MB internal flash

  // Name as the tail, unlength-prefixed: the frame already carries the length.
  load_device_name();
  uint8_t nlen = strlen(bridge_name);
  memcpy(p, bridge_name, nlen); p += nlen;
  return p - out;
}

void sys_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_SYS, op);
  switch (op) {
    case 0x01:  // PING: echo payload
      proto_reply(seq, cmd, p, len);
      break;

    case 0x02: {  // INFO
      uint8_t buf[64];
      proto_reply(seq, cmd, buf, sys_build_info(buf));
      break;
    }

    case 0x03:  // SET_BAUD: native USB CDC ignores baud — acknowledge without acting
      NEED(4);
      proto_reply_ok(seq, cmd);
      break;

    case 0x04:  // RESET
      proto_reply_ok(seq, cmd);
      proto_tx_flush();
      delay(50);
      NVIC_SystemReset();
      break;

    case 0x06: {  // SET_NAME: payload = name (0..BRIDGE_NAME_MAX bytes), persisted to LittleFS
      if (len > BRIDGE_NAME_MAX) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      memcpy(bridge_name, p, len);
      bridge_name[len] = 0;
      name_loaded = true;
      if (nrf_internalfs_begin()) {
        File f = InternalFS.open(NAME_PATH, FILE_O_WRITE);
        if (f) { f.truncate(0); f.seek(0); if (len) f.write(p, len); f.close(); }
      }
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x05: {  // FREE_HEAP -> free, min_free, largest, dropped_evts, rx_dropped, serial_errs
      uint8_t buf[24];
      uint32_t freeb = plat_free_heap();
      static uint32_t min_free = 0xFFFFFFFFUL;  // low-water sampled at each call (no min-ever counter on this core)
      if (freeb < min_free) min_free = freeb;
      write_be32(buf, freeb);
      write_be32(buf + 4, min_free);
      write_be32(buf + 8, freeb);  // no largest-block query; total free is a safe proxy
      write_be32(buf + 12, proto_dropped_events());
      write_be32(buf + 16, link_ble_rx_dropped());
      write_be32(buf + 20, link_serial_rx_errors());
      proto_reply(seq, cmd, buf, 24);
      break;
    }

    case 0x0B: {  // LINK_POWER: mode u8 (0=performance, 1=battery) — BLE conn params
      NEED(1);
      if (p[0] > 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (!link_ble_power(p[0] == 1)) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x08: {  // SLEEP: mode u8 (0 deep,1 light)|us u64|wake_pin i8|wake_level u8
      NEED(11);
      uint8_t mode = p[0];
      int8_t wpin = (int8_t)p[9];
      uint8_t wlevel = p[10] ? 1 : 0;
      // nRF System OFF (deep sleep) wakes only via reset / GPIO DETECT — there is
      // no timer wake in System OFF, and FreeRTOS+SoftDevice make a resumable
      // "light sleep" impractical. So: deep sleep requires a wake pin; light
      // sleep is unsupported.
      if (mode != 0 || wpin < 0) { proto_reply_err(seq, cmd, ST_UNSUPPORTED); return; }
      proto_reply_ok(seq, cmd);
      proto_tx_flush();
      delay(50);
      // Configure the wake pin to latch a level (SENSE) so DETECT triggers
      // System OFF wake (which resets the chip — link drops, board reboots).
      uint32_t nrfpin = g_ADigitalPinMap[wpin];
      nrf_gpio_cfg_sense_input(nrfpin,
          wlevel ? NRF_GPIO_PIN_PULLDOWN : NRF_GPIO_PIN_PULLUP,
          wlevel ? NRF_GPIO_PIN_SENSE_HIGH : NRF_GPIO_PIN_SENSE_LOW);
      sd_power_system_off();   // SoftDevice is up (Bluefruit): must route through the SD
      NRF_POWER->SYSTEMOFF = 1; // fallback if the SD call returns (SD disabled)
      while (1) {}
      break;
    }

    case 0x09: {  // WAKE_CAUSE -> cause u8 (7 = GPIO wake from System OFF, else 0)
      uint8_t cause = (plat_resetreas() & POWER_RESETREAS_OFF_Msk) ? 7 : 0;
      proto_reply(seq, cmd, &cause, 1);
      break;
    }

    case 0x0A:  // CPU_FREQ: nRF52840 runs at a fixed 64 MHz
      proto_reply_err(seq, cmd, ST_UNSUPPORTED);
      break;

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#endif  // ARDUINO_ARCH_NRF52
