#if defined(ARDUINO_ARCH_ESP32)
// Wi-Fi module: STA + AP management and async network scanning.
// Callbacks never write to serial directly — they enqueue events via
// proto_send_event so that tx_task remains the sole serial writer.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/radio.h"
#include <WiFi.h>

static bool scanning = false;

// Radio stays off until the host sends its first Wi-Fi command.
// This matters on BLE-only boards: the Wi-Fi driver costs several KB of heap,
// and we only pay that cost if Wi-Fi is actually used. Ownership and heap
// guards live in radio.cpp; this module just claims RADIO_STA / RADIO_AP.
// Power-save and coex settings are left at IDF defaults.
// IMPORTANT: never set WIFI_PS_NONE while Bluetooth is active — the SW radio
// arbiter becomes unstable and packet loss increases dramatically.

static void on_wifi_event(WiFiEvent_t event, WiFiEventInfo_t info) {
  uint8_t buf[5] = {0};
  switch (event) {
    case ARDUINO_EVENT_WIFI_STA_CONNECTED:
      buf[0] = 1;
      break;
    case ARDUINO_EVENT_WIFI_STA_GOT_IP: {
      buf[0] = 2;
      uint32_t ip = (uint32_t)WiFi.localIP();
      memcpy(buf + 1, &ip, 4);  // IP in network byte order (big-endian, as received)
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
  WiFi.persistent(false);  // never write credentials to flash — avoid flash wear and leftover secrets
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
    radio_release(0);  // scan held no bit: power off unless someone owns the radio
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
  // The scan raised the radio without claiming a user bit; give the driver
  // heap back unless STA/AP/ESP-NOW still owns the radio.
  radio_release(0);
}

// Read a length-prefixed string from the payload at position *p.
// Advances *p and decrements *left by the amount consumed.
// Returns false if the payload is too short or the string would overflow `out`.
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
      // Channel-hopping during a scan disrupts ESP-NOW: peers on other channels
      // cannot be reached while the radio sweeps. See PROTOCOL.md for details.
      if (scanning) { proto_reply_err(seq, cmd, ST_BUSY); return; }
      if (!radio_acquire(0)) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
      WiFi.mode(radio_held(RADIO_AP) ? WIFI_MODE_APSTA : WIFI_MODE_STA);
      if (WiFi.scanNetworks(true, true) == WIFI_SCAN_FAILED) {
        radio_release(0);  // scan never started: don't leave an unowned radio up
        proto_reply_err(seq, cmd, ST_WIFI);
        return;
      }
      scanning = true;
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x02: {  // CONNECT: ssid, pass — begins async association; poll STATUS or wait for STATE_EVT
      char ssid[33], pass[65];
      const uint8_t* q = p; uint16_t left = len;
      if (!take_str(q, left, ssid, sizeof(ssid)) || !take_str(q, left, pass, sizeof(pass))) {
        proto_reply_err(seq, cmd, ST_BAD_ARGS);
        return;
      }
      if (!radio_acquire(RADIO_STA)) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
      WiFi.mode(radio_held(RADIO_AP) ? WIFI_MODE_APSTA : WIFI_MODE_STA);
      WiFi.begin(ssid, pass[0] ? pass : nullptr);
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x03:  // DISCONNECT
      // wifioff=false: radio_release decides the power-off; dropping STA mode
      // here would kill the STA interface a co-resident ESP-NOW rides on.
      WiFi.disconnect(false, false);
      radio_release(RADIO_STA);  // radio powers off unless AP or ESP-NOW still holds it
      proto_reply_ok(seq, cmd);
      break;

    case 0x04: {  // STATUS -> status, ip[4], gw[4], mask[4], rssi i8, channel, mac[6]
      uint8_t buf[21];
      wl_status_t st = WiFi.status();
      buf[0] = (uint8_t)st;
      uint32_t ip = (uint32_t)WiFi.localIP();
      uint32_t gw = (uint32_t)WiFi.gatewayIP();
      uint32_t mask = (uint32_t)WiFi.subnetMask();
      memcpy(buf + 1, &ip, 4);
      memcpy(buf + 5, &gw, 4);
      memcpy(buf + 9, &mask, 4);
      buf[13] = (uint8_t)(int8_t)(st == WL_CONNECTED ? WiFi.RSSI() : 0);
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
      if (!radio_acquire(RADIO_AP)) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
      // Keep the STA interface up alongside the AP when anything needs it —
      // ESP-NOW sends through WIFI_IF_STA and dies in pure-AP mode.
      WiFi.mode(radio_held(RADIO_STA) || radio_held(RADIO_ESPNOW)
                    ? WIFI_MODE_APSTA : WIFI_MODE_AP);
      if (!WiFi.softAP(ssid, pass[0] ? pass : nullptr, channel, 0, max_conn)) {
        radio_release(RADIO_AP);
        proto_reply_err(seq, cmd, ST_WIFI);
        return;
      }
      uint32_t ip = (uint32_t)WiFi.softAPIP();
      proto_reply(seq, cmd, (uint8_t*)&ip, 4);
      break;
    }

    case 0x06:  // AP_STOP
      WiFi.softAPdisconnect(true);
      radio_release(RADIO_AP);
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
#endif  // ARDUINO_ARCH_ESP32
