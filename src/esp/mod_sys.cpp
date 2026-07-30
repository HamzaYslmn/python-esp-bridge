#if defined(ARDUINO_ARCH_ESP32)
// SYS: ping, info, baud switch, reset, heap stats, device name.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/link.h"
#include "espbridge/radio.h"
#include <esp_mac.h>
#include <esp_heap_caps.h>
#include <esp_sleep.h>
#include <driver/gpio.h>
#include <Preferences.h>

// Device name persisted in NVS, so a host can say Bridge("relays") instead of
// reading MACs off boards. Loaded lazily on first use.
static char bridge_name[BRIDGE_NAME_MAX + 1];
static bool name_loaded = false;

static void load_device_name() {
  if (name_loaded) return;
  Preferences prefs;
  if (prefs.begin("bridge", true)) {  // read-only; absent on first boot (name stays empty)
    prefs.getString("name", "").toCharArray(bridge_name, sizeof(bridge_name));
    prefs.end();
  }
  name_loaded = true;
}

const char* sys_device_name() {
  load_device_name();
  return bridge_name;
}

uint16_t sys_build_info(uint8_t* out) {
  uint8_t* p = out;
  *p++ = PROTOCOL_VERSION;
  *p++ = FW_VERSION_MAJOR;
  *p++ = FW_VERSION_MINOR;
  *p++ = FW_VERSION_PATCH;
  *p++ = BRIDGE_CHIP;
  *p++ = (uint8_t)ESP.getChipRevision();

  uint8_t mac[6] = {0};
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  memcpy(p, mac, 6); p += 6;

  // Every BRIDGE_* flag is defined to 0 or 1 by config.h (never left undefined),
  // so this folds at compile time exactly like the #if-per-capability ladder it
  // replaces — and reads as the flag -> bit table it actually is.
  uint32_t caps = CAP_WIFI
      | CAPIF(BRIDGE_HAS_DAC,     CAP_DAC)
      | CAPIF(BRIDGE_HAS_TOUCH,   CAP_TOUCH)
      | CAPIF(BRIDGE_HAS_BT_CLASSIC, CAP_BT_CLASSIC)
      | CAPIF(BRIDGE_HAS_BLE,     CAP_BLE)
      | CAPIF(BRIDGE_BLE,         CAP_BLE_FW)
      | CAPIF(BRIDGE_NATIVE_USB,  CAP_NATIVE_USB)
      | CAPIF(BRIDGE_HAS_ESPNOW,  CAP_ESPNOW)
      | CAPIF(BRIDGE_HAS_RMT,     CAP_RMT)
      | CAPIF(BRIDGE_HAS_ONEWIRE, CAP_ONEWIRE)
      | CAPIF(BRIDGE_HAS_TWAI,    CAP_TWAI)
      | CAPIF(BRIDGE_HAS_I2S,     CAP_I2S)
      | CAPIF(BRIDGE_HAS_FS,      CAP_FS)
      | CAPIF(BRIDGE_HAS_NVS,     CAP_NVS)
      | CAPIF(BRIDGE_HAS_OTA,     CAP_OTA)
      | CAPIF(BRIDGE_ETH,         CAP_ETH)
      | CAPIF(BRIDGE_HAS_MCPWM,   CAP_MCPWM)
      | CAPIF(BRIDGE_HAS_SLEEP,   CAP_SLEEP)
      | CAPIF(BRIDGE_WIFI_LINK,   CAP_WIFI_LINK);  // built in; LINK_STATUS says if it runs
  // Runtime, not build-time: PSRAM presence and whether the BLE link came up.
  if (psramFound()) caps |= CAP_PSRAM | CAPIF(BRIDGE_CAM, CAP_CAM);  // cam needs PSRAM
  if (link_ble_enabled()) caps |= CAP_BLE_LINK;
  write_be32(p, caps); p += 4;

  *p++ = SOC_GPIO_PIN_COUNT;
  *p++ = (uint8_t)(ESP.getFlashChipSize() / (1024UL * 1024UL));

  // Name as the tail, unlength-prefixed: the frame already carries the length,
  // so "the rest of the payload" needs no byte of its own.
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

    case 0x03: {  // SET_BAUD
      NEED(4);
      uint32_t baud = read_be32(p);
#if BRIDGE_NATIVE_USB
      (void)baud;
      proto_reply_ok(seq, cmd);  // Native USB CDC ignores the baud rate entirely — acknowledge without acting
#else
      if (baud < 9600 || baud > 3000000) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      proto_reply_ok(seq, cmd);  // send the ACK at the current (old) baud rate...
      proto_tx_flush();          // ...and make sure it's fully transmitted before we switch
      delay(20);                 // give the host's UART time to drain and re-lock at the new rate
      Serial.updateBaudRate(baud);
#endif
      break;
    }

    case 0x04:  // RESET
      proto_reply_ok(seq, cmd);
      proto_tx_flush();
      delay(50);
      ESP.restart();
      break;

    case 0x06: {  // SET_NAME: payload = name (0..BRIDGE_NAME_MAX bytes), persisted in NVS
      if (len > BRIDGE_NAME_MAX) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      Preferences prefs;
      if (!prefs.begin("bridge", false)) { proto_reply_err(seq, cmd, ST_IO); return; }
      memcpy(bridge_name, p, len);
      bridge_name[len] = 0;
      name_loaded = true;
      prefs.putString("name", bridge_name);
      prefs.end();
      proto_reply_ok(seq, cmd);
      break;
    }

#if BRIDGE_HAS_SLEEP
    case 0x08: {  // SLEEP: mode u8 (0=deep, 1=light) | us u64 | wake_pin i8 | wake_level u8
      NEED(11);
      uint8_t mode = p[0];
      uint64_t us = ((uint64_t)read_be32(p + 1) << 32) | read_be32(p + 5);
      int8_t wpin = (int8_t)p[9];
      uint8_t wlevel = p[10] ? 1 : 0;
      if (mode > 1 || (us == 0 && wpin < 0)) {  // reject: no wakeup source means the device would sleep forever
        proto_reply_err(seq, cmd, ST_BAD_ARGS);
        return;
      }
      if (us) esp_sleep_enable_timer_wakeup(us);
      if (wpin >= 0) {
#if SOC_PM_SUPPORT_EXT0_WAKEUP
        esp_sleep_enable_ext0_wakeup((gpio_num_t)wpin, wlevel);  // EXT0 wakeup requires an RTC-capable GPIO
#else  // C3/C6 lack EXT0; they use a different GPIO wakeup API that also differs between deep and light sleep
        if (mode == 0) {
          esp_deep_sleep_enable_gpio_wakeup(1ULL << wpin,
              wlevel ? ESP_GPIO_WAKEUP_GPIO_HIGH : ESP_GPIO_WAKEUP_GPIO_LOW);
        } else {
          gpio_wakeup_enable((gpio_num_t)wpin,
              wlevel ? GPIO_INTR_HIGH_LEVEL : GPIO_INTR_LOW_LEVEL);
          esp_sleep_enable_gpio_wakeup();
        }
#endif
      }
      if (mode == 0) {  // deep sleep: send ACK, flush, then sleep — device reboots on wake (no resume)
        proto_reply_ok(seq, cmd);
        proto_tx_flush();
        delay(50);
        esp_deep_sleep_start();
      } else {  // light sleep: execution pauses here and resumes on wake; reply is sent after waking
        esp_light_sleep_start();
        uint8_t cause = (uint8_t)esp_sleep_get_wakeup_cause();
        proto_reply(seq, cmd, &cause, 1);
      }
      break;
    }
#else
    case 0x08:  // SLEEP disabled: sleep entry code must be IRAM-resident, which conflicts with classic BT + BLE builds
      proto_reply_err(seq, cmd, ST_UNSUPPORTED);
      break;
#endif

    // WAKE_CAUSE is always available regardless of the BRIDGE_HAS_SLEEP gate.
    // Reading the wakeup cause is a cheap RTC register read that requires no IRAM-resident sleep code,
    // so it is safe even in classic BT + BLE builds where sleep entry is disabled.
    case 0x09: {  // WAKE_CAUSE -> cause u8 (0 = normal power-on/reset)
      uint8_t cause = (uint8_t)esp_sleep_get_wakeup_cause();
      proto_reply(seq, cmd, &cause, 1);
      break;
    }

    case 0x0A: {  // CPU_FREQ: mhz u8 (80|160|240) -> mhz u8
      NEED(1);
      uint8_t mhz = p[0];
      // 80 MHz is the floor while any radio is on; APB stays 80 MHz so UART
      // and peripheral clocks are unaffected.
      if (mhz != 80 && mhz != 160 && mhz != 240) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (!setCpuFrequencyMhz(mhz)) { proto_reply_err(seq, cmd, ST_IO); return; }
      uint8_t now = (uint8_t)getCpuFrequencyMhz();
      proto_reply(seq, cmd, &now, 1);
      break;
    }

    case 0x0B: {  // LINK_POWER: mode u8 (0=performance, 1=battery) — BLE conn params
      NEED(1);
      if (p[0] > 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (!link_ble_power(p[0] == 1)) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x0C: {  // RADIO_OFF — routed to slow_task (see dispatch()), next to every radio user
      // The host tears down its own Wi-Fi/ESP-NOW/AP first (those releases
      // already power the Wi-Fi driver off); this refuses rather than yanking
      // a live radio out from under a module — predictable beats forceful.
      if (radio_active()) { proto_reply_err(seq, cmd, ST_BUSY); return; }         // Wi-Fi/ESP-NOW still up
      if (ble_module_active()) { proto_reply_err(seq, cmd, ST_BUSY); return; }    // esp.ble used: reboot first
      if (!link_ble_shutdown()) { proto_reply_err(seq, cmd, ST_BUSY); return; }   // a BLE central is connected
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x05: {  // FREE_HEAP -> free, min_free, largest, dropped_evts, rx_dropped, serial_errs
      uint8_t buf[24];
      write_be32(buf, ESP.getFreeHeap());
      write_be32(buf + 4, ESP.getMinFreeHeap());
      write_be32(buf + 8, heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
      write_be32(buf + 12, proto_dropped_events());
      write_be32(buf + 16, link_ble_rx_dropped());    // added fw 0.3.2; host length-checks
      write_be32(buf + 20, link_serial_rx_errors());  // added fw 0.5.2; host length-checks
      proto_reply(seq, cmd, buf, 24);
      break;
    }

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}
#endif  // ARDUINO_ARCH_ESP32
