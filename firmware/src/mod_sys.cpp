// SYS: ping, info, baud switch, reset, heap stats, persistent device name.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/link.h"
#include <esp_mac.h>
#include <esp_heap_caps.h>
#include <Preferences.h>

// User-assigned device name (NVS-persisted) so hosts with several bridges can
// address boards by name instead of fragile port paths.
static char bridge_name[BRIDGE_NAME_MAX + 1];
static bool name_loaded = false;

static void load_name() {
  if (name_loaded) return;
  Preferences prefs;
  if (prefs.begin("bridge", true)) {  // read-only; fails if namespace absent
    String n = prefs.getString("name", "");
    strlcpy(bridge_name, n.c_str(), sizeof(bridge_name));
    prefs.end();
  }
  name_loaded = true;
}

const char* sys_device_name() {
  load_name();
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

  uint32_t caps = CAP_WIFI;
#if BRIDGE_HAS_DAC
  caps |= CAP_DAC;
#endif
#if BRIDGE_HAS_TOUCH
  caps |= CAP_TOUCH;
#endif
#if BRIDGE_HAS_BT_CLASSIC
  caps |= CAP_BT_CLASSIC;
#endif
#if BRIDGE_HAS_BLE
  caps |= CAP_BLE;
#endif
#if BRIDGE_BLE
  caps |= CAP_BLE_FW;
#endif
#if BRIDGE_NATIVE_USB
  caps |= CAP_NATIVE_USB;
#endif
#if BRIDGE_HAS_ESPNOW
  caps |= CAP_ESPNOW;
#endif
  if (psramFound()) caps |= CAP_PSRAM;
  if (link_ble_enabled()) caps |= CAP_BLE_LINK;
  wr32(p, caps); p += 4;

  *p++ = SOC_GPIO_PIN_COUNT;
  *p++ = (uint8_t)(ESP.getFlashChipSize() / (1024UL * 1024UL));

  load_name();
  uint8_t nlen = strlen(bridge_name);
  *p++ = nlen;
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
      if (len < 4) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      uint32_t baud = rd32(p);
#if BRIDGE_NATIVE_USB
      (void)baud;
      proto_reply_ok(seq, cmd);  // USB CDC: baud is meaningless, accept as no-op
#else
      if (baud < 9600 || baud > 3000000) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      proto_reply_ok(seq, cmd);  // reply at the OLD baud...
      proto_tx_flush();          // ...wait until it is truly on the wire
      delay(20);                 // let the host's UART drain
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

    case 0x06: {  // SET_NAME: payload = name (0..32 bytes), persisted in NVS
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

    case 0x05: {  // FREE_HEAP
      uint8_t buf[16];
      wr32(buf, ESP.getFreeHeap());
      wr32(buf + 4, ESP.getMinFreeHeap());
      wr32(buf + 8, heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
      wr32(buf + 12, proto_dropped_events());
      proto_reply(seq, cmd, buf, 16);
      break;
    }

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}
