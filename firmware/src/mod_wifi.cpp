// Wi-Fi: STA + AP management, async scan. Handlers and polling run on
// net_task; WiFi event callbacks run on the WiFi event task — both just
// enqueue frames via proto_send_event (tx_task owns the serial port).
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include <WiFi.h>

static bool scanning = false;
static bool ap_active = false;
static bool sta_started = false;
static bool coex_pinned = false;  // Wi-Fi pre-inited for BLE coex: never drop to MODE_NULL

bool wifi_is_active() {
  return WiFi.getMode() != WIFI_MODE_NULL;
}

// Classic-ESP32 coexistence: the Wi-Fi driver must come up BEFORE Bluedroid
// so the coex arbiter sees the radios in the right order (Wi-Fi -> BLE).
// Called from setup() ahead of link_ble_init() when BRIDGE_WIFI_COEX is set.
// Power save stays at the Arduino default WIFI_PS_MIN_MODEM — customizing it
// (especially WIFI_PS_NONE) destabilizes BLE coexistence per the IDF guide.
void wifi_coex_preinit() {
  WiFi.mode(WIFI_STA);
  coex_pinned = true;
  proto_log_heap("coex: wifi up");
}

static void on_wifi_event(WiFiEvent_t event, WiFiEventInfo_t info) {
  uint8_t buf[5] = {0};
  switch (event) {
    case ARDUINO_EVENT_WIFI_STA_CONNECTED:
      buf[0] = 1;
      break;
    case ARDUINO_EVENT_WIFI_STA_GOT_IP: {
      buf[0] = 2;
      uint32_t ip = (uint32_t)WiFi.localIP();
      memcpy(buf + 1, &ip, 4);  // IPAddress stores network byte order
      break;
    }
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
      buf[0] = 3;
      break;
    default:
      return;
  }
  proto_send_event(WIFI_STATE_EVT, buf, 5);
}

void wifi_init() {
  WiFi.persistent(false);  // never wear flash with credentials
  WiFi.onEvent(on_wifi_event);
}

void wifi_poll() {
  if (!scanning) return;
  int16_t n = WiFi.scanComplete();
  if (n == WIFI_SCAN_RUNNING) return;
  scanning = false;
  if (n < 0) {  // WIFI_SCAN_FAILED
    uint8_t zero = 0;
    proto_send_event(WIFI_SCAN_DONE, &zero, 1);
    return;
  }
  uint8_t count = n > 255 ? 255 : (uint8_t)n;
  for (uint8_t i = 0; i < count; i++) {
    String ssid = WiFi.SSID(i);
    uint8_t slen = ssid.length() > 32 ? 32 : ssid.length();
    uint8_t buf[12 + 32];
    buf[0] = i;
    buf[1] = count;
    buf[2] = (int8_t)WiFi.RSSI(i);
    buf[3] = (uint8_t)WiFi.encryptionType(i);
    buf[4] = (uint8_t)WiFi.channel(i);
    uint8_t* bssid = WiFi.BSSID(i);
    if (bssid) memcpy(buf + 5, bssid, 6); else memset(buf + 5, 0, 6);
    buf[11] = slen;
    memcpy(buf + 12, ssid.c_str(), slen);
    proto_send_event(WIFI_SCAN_RES, buf, 12 + slen);
  }
  proto_send_event(WIFI_SCAN_DONE, &count, 1);
  WiFi.scanDelete();
}

// payload cursor helper for length-prefixed strings
static bool take_str(const uint8_t*& p, uint16_t& left, char* out, uint8_t cap) {
  if (left < 1) return false;
  uint8_t n = *p++; left--;
  if (n >= cap || left < n) return false;
  memcpy(out, p, n);
  out[n] = 0;
  p += n; left -= n;
  return true;
}

void wifi_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_WIFI, op);
  switch (op) {
    case 0x01: {  // SCAN (async)
      // Quirk: a scan hops channels, so ESP-NOW packets are dropped while it
      // runs. Known trade-off; documented in PROTOCOL.md.
      if (scanning) { proto_reply_err(seq, cmd, ST_BUSY); return; }
      WiFi.mode(ap_active ? WIFI_MODE_APSTA : WIFI_MODE_STA);
      if (WiFi.scanNetworks(true, true) == WIFI_SCAN_FAILED) {
        proto_reply_err(seq, cmd, ST_WIFI);
        return;
      }
      scanning = true;
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x02: {  // CONNECT: ssid, pass (async; poll STATUS or wait STATE_EVT)
      char ssid[33], pass[65];
      const uint8_t* q = p; uint16_t left = len;
      if (!take_str(q, left, ssid, sizeof(ssid)) || !take_str(q, left, pass, sizeof(pass))) {
        proto_reply_err(seq, cmd, ST_BAD_ARGS);
        return;
      }
      WiFi.mode(ap_active ? WIFI_MODE_APSTA : WIFI_MODE_STA);
      WiFi.begin(ssid, pass[0] ? pass : nullptr);
      sta_started = true;
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x03:  // DISCONNECT
      WiFi.disconnect(true /*wifioff if no AP*/, false);
      sta_started = false;
      // Keep the radio in STA mode while ESP-NOW rides on it or the driver is
      // coex-pinned (turning it off would break espnow / the BLE coex order).
      if (!ap_active && !espnow_is_active() && !coex_pinned) WiFi.mode(WIFI_MODE_NULL);
      proto_reply_ok(seq, cmd);
      break;

    case 0x04: {  // STATUS -> status, ip[4], gw[4], mask[4], rssi i8, channel, mac[6]
      uint8_t buf[21];
      buf[0] = (uint8_t)WiFi.status();
      uint32_t ip = (uint32_t)WiFi.localIP();
      uint32_t gw = (uint32_t)WiFi.gatewayIP();
      uint32_t mask = (uint32_t)WiFi.subnetMask();
      memcpy(buf + 1, &ip, 4);
      memcpy(buf + 5, &gw, 4);
      memcpy(buf + 9, &mask, 4);
      buf[13] = (uint8_t)(int8_t)(WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0);
      buf[14] = (uint8_t)WiFi.channel();
      WiFi.macAddress(buf + 15);
      proto_reply(seq, cmd, buf, 21);
      break;
    }

    case 0x05: {  // AP_START: ssid, pass, channel, max_conn -> ip[4]
      char ssid[33], pass[65];
      const uint8_t* q = p; uint16_t left = len;
      if (!take_str(q, left, ssid, sizeof(ssid)) || !take_str(q, left, pass, sizeof(pass)) ||
          left < 2) {
        proto_reply_err(seq, cmd, ST_BAD_ARGS);
        return;
      }
      uint8_t channel = q[0] ? q[0] : 1;
      uint8_t max_conn = q[1] ? q[1] : 4;
      WiFi.mode(sta_started ? WIFI_MODE_APSTA : WIFI_MODE_AP);
      if (!WiFi.softAP(ssid, pass[0] ? pass : nullptr, channel, 0, max_conn)) {
        proto_reply_err(seq, cmd, ST_WIFI);
        return;
      }
      ap_active = true;
      uint32_t ip = (uint32_t)WiFi.softAPIP();
      proto_reply(seq, cmd, (uint8_t*)&ip, 4);
      break;
    }

    case 0x06:  // AP_STOP
      WiFi.softAPdisconnect(true);
      ap_active = false;
      if (!sta_started && !espnow_is_active() && !coex_pinned) WiFi.mode(WIFI_MODE_NULL);
      proto_reply_ok(seq, cmd);
      break;

    case 0x07: {  // HOSTNAME
      char name[33];
      const uint8_t* q = p; uint16_t left = len;
      if (!take_str(q, left, name, sizeof(name))) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      WiFi.setHostname(name);
      proto_reply_ok(seq, cmd);
      break;
    }

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}
