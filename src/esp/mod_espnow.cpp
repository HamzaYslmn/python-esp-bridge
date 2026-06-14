#if defined(ARDUINO_ARCH_ESP32)
// ESP-NOW: connectionless 2.4 GHz messaging, coexisting with Wi-Fi STA/AP and BLE
// via the IDF SW radio arbiter. Callbacks only enqueue events (proto_send_event);
// tx_task stays the sole serial writer.
//
// Coexistence rules (from the IDF guide — do NOT "optimize" without understanding
// the arbiter): never esp_wifi_set_ps(WIFI_PS_NONE) while BT is active (default
// MIN_MODEM keeps the arbiter stable); never call esp_coex_* manually; a connected
// STA / active AP owns the channel and ESP-NOW inherits it — only lock a channel
// when the radio is idle.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/radio.h"

#if BRIDGE_HAS_ESPNOW

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_mac.h>
#include <atomic>

static bool inited = false;

// Send-completion tracking. ESP-NOW serializes sends (callbacks arrive in call
// order), so ff_outstanding counts in-flight fire-and-forget sends: a callback
// with ff_outstanding > 0 is fire-and-forget (emit ESPNOW_SEND_EVT, decrement);
// at 0 it's the blocking sync send (store result, give the semaphore).
static SemaphoreHandle_t tx_done_sem;
static std::atomic<uint8_t> ff_outstanding{0};       // fire-and-forget sends in flight
static std::atomic<uint8_t> sync_status{1};          // last sync result (0 = ACKed)

static void handle_tx_done(const uint8_t* dst_mac, esp_now_send_status_t status) {
  if (ff_outstanding > 0) {
    ff_outstanding--;
    if (!dst_mac) return;
    uint8_t buf[7];
    memcpy(buf, dst_mac, 6);
    buf[6] = status == ESP_NOW_SEND_SUCCESS ? 0 : 1;
    proto_send_event(ESPNOW_SEND_EVT, buf, 7);
    return;
  }
  sync_status = status == ESP_NOW_SEND_SUCCESS ? 0 : 1;
  if (tx_done_sem) xSemaphoreGive(tx_done_sem);
}

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)
static void on_send(const wifi_tx_info_t* info, esp_now_send_status_t status) {
  handle_tx_done(info ? info->des_addr : nullptr, status);
}
#else
static void on_send(const uint8_t* mac, esp_now_send_status_t status) {
  handle_tx_done(mac, status);
}
#endif

static void on_recv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
  if (!info || len > ESPNOW_MAX_DATA) return;
  uint8_t buf[7 + ESPNOW_MAX_DATA];
  memcpy(buf, info->src_addr, 6);
  buf[6] = (uint8_t)(info->rx_ctrl ? info->rx_ctrl->rssi : 0);
  if (len) memcpy(buf + 7, data, len);
  proto_send_event(ESPNOW_RX_EVT, buf, 7 + len);
}

static uint8_t map_espnow_error(esp_err_t e) {
  switch (e) {
    case ESP_OK:                     return ST_OK;
    case ESP_ERR_ESPNOW_NOT_INIT:    return ST_NOT_INIT;
    case ESP_ERR_ESPNOW_NO_MEM:      return ST_NO_MEM;
    case ESP_ERR_ESPNOW_ARG:
    case ESP_ERR_ESPNOW_NOT_FOUND:   return ST_BAD_ARGS;
    default:                         return ST_IO;
  }
}

// INIT: channel u8 (0 = auto/inherit) | flags u8 (bit0 = 802.11 LR long range)
static void do_init(uint8_t seq, uint16_t cmd, const uint8_t* p, uint16_t len) {
  NEED(2);
  uint8_t channel = p[0], flags = p[1];

  // ESP-NOW requires the Wi-Fi driver running and sends through the STA
  // interface, even when not associated. radio_acquire applies the BLE heap
  // guard before any bring-up.
  if (!radio_acquire(RADIO_ESPNOW)) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
  if (WiFi.getMode() == WIFI_MODE_NULL) WiFi.mode(WIFI_STA);
  else if (WiFi.getMode() == WIFI_MODE_AP) WiFi.mode(WIFI_MODE_APSTA);

  uint8_t proto = WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N;
  if (flags & 0x01) proto |= WIFI_PROTOCOL_LR;
  esp_wifi_set_protocol(WIFI_IF_STA, proto);

  // If a STA is associated or an AP is active, they own the channel and ESP-NOW
  // inherits it — we must not override it. Only set the channel when the radio
  // is idle (no association, no AP).
  bool channel_owned = WiFi.status() == WL_CONNECTED || (WiFi.getMode() & WIFI_MODE_AP);
  if (!channel_owned) {
    esp_wifi_set_channel(channel ? channel : 1, WIFI_SECOND_CHAN_NONE);
  } else if (channel && channel != WiFi.channel()) {
    proto_log(1, "espnow: channel follows Wi-Fi while connected; request ignored");
  }

  if (!inited) {
    esp_err_t e = esp_now_init();
    if (e != ESP_OK) { radio_release(RADIO_ESPNOW); proto_reply_err(seq, cmd, map_espnow_error(e)); return; }
    // Max wake window: with Wi-Fi idle the coex arbiter favours BLE and
    // connectionless power save gates the receiver — broadcasts (single-shot,
    // never retried) land in dead air for seconds at a time without this.
    esp_now_set_wake_window(65535);
    if (!tx_done_sem) tx_done_sem = xSemaphoreCreateBinary();
    esp_now_register_recv_cb(on_recv);
    esp_now_register_send_cb(on_send);
    inited = true;
  }

  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  proto_reply(seq, cmd, mac, 6);
}

// ADD_PEER: mac[6] | channel u8 (0 = follow current) | encrypt u8 | [lmk[16]]
static void do_add_peer(uint8_t seq, uint16_t cmd, const uint8_t* p, uint16_t len) {
  if (len != 8 && len != 8 + 16) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
  bool encrypt = p[7] != 0;
  if (encrypt && len != 8 + 16) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, p, 6);
  peer.channel = p[6];          // 0 = follow the current Wi-Fi channel
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = encrypt;
  if (encrypt) memcpy(peer.lmk, p + 8, 16);

  esp_err_t e = esp_now_is_peer_exist(peer.peer_addr) ? esp_now_mod_peer(&peer)
                                                      : esp_now_add_peer(&peer);
  if (e != ESP_OK) { proto_reply_err(seq, cmd, map_espnow_error(e)); return; }
  proto_reply_ok(seq, cmd);
}

// SEND: mac[6] | data (up to 250 bytes).
//
// Two modes depending on the sequence number in the frame header:
//   seq != 0 — synchronous: block until the TX callback fires, then reply with
//              a "delivered" byte (1 = peer MAC-layer ACKed, 0 = not ACKed).
//              Note: broadcast addresses never produce an ACK, so delivered will
//              always be 0 for broadcasts.
//   seq == 0 — fire-and-forget: send without waiting; the result arrives later
//              as an ESPNOW_SEND_EVT unsolicited event.
static void do_send(uint8_t seq, uint16_t cmd, const uint8_t* p, uint16_t len) {
  if (len < 6 || len > 6 + ESPNOW_MAX_DATA) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }

  if (seq == 0) {
    ff_outstanding++;
    if (esp_now_send(p, p + 6, len - 6) != ESP_OK) ff_outstanding--;
    return;  // no reply for fire-and-forget
  }

  while (xSemaphoreTake(tx_done_sem, 0) == pdTRUE) {}  // drain any leftover gives from a previous send
  esp_err_t e = esp_now_send(p, p + 6, len - 6);
  if (e != ESP_OK) { proto_reply_err(seq, cmd, map_espnow_error(e)); return; }
  uint8_t delivered = 0;
  if (xSemaphoreTake(tx_done_sem, pdMS_TO_TICKS(25)) == pdTRUE)
    delivered = sync_status == 0 ? 1 : 0;
  proto_reply(seq, cmd, &delivered, 1);
}

void espnow_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_ESPNOW, op);
  if (!inited && op != 0x01) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
  switch (op) {
    case 0x01:  // INIT
      do_init(seq, cmd, p, len);
      break;

    case 0x02:  // DEINIT — radio powers off unless STA or AP still holds it
      esp_now_deinit();
      inited = false;
      ff_outstanding = 0;
      radio_release(RADIO_ESPNOW);
      proto_reply_ok(seq, cmd);
      break;

    case 0x03:  // SET_PMK: pmk[16]
      if (len != 16) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (esp_now_set_pmk(p) != ESP_OK) { proto_reply_err(seq, cmd, ST_IO); return; }
      proto_reply_ok(seq, cmd);
      break;

    case 0x04:  // ADD_PEER
      do_add_peer(seq, cmd, p, len);
      break;

    case 0x05: {  // DEL_PEER: mac[6]
      if (len != 6) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      esp_err_t e = esp_now_del_peer(p);
      if (e != ESP_OK) { proto_reply_err(seq, cmd, map_espnow_error(e)); return; }
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x06:  // SEND
      do_send(seq, cmd, p, len);
      break;

    case 0x07: {  // POWER_SAVE: wake window u16 ms | wake interval u16 ms (0 = keep)
      // RF listens `window` ms per `interval`; 0 = RX off (TX still works).
      NEED(4);
      uint16_t window   = read_be16(p);
      uint16_t interval = read_be16(p + 2);
      esp_err_t e = esp_now_set_wake_window(window);
      if (e == ESP_OK && interval) e = esp_wifi_connectionless_module_set_wake_interval(interval);
      if (e != ESP_OK) { proto_reply_err(seq, cmd, map_espnow_error(e)); return; }
      proto_reply_ok(seq, cmd);
      break;
    }

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#else  // !BRIDGE_HAS_ESPNOW

UNSUPPORTED_STUB(espnow_handle, MOD_ESPNOW)

#endif  // BRIDGE_HAS_ESPNOW
#endif  // ARDUINO_ARCH_ESP32
