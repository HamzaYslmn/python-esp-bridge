// ESP-NOW: connectionless 2.4 GHz messaging, coexisting with Wi-Fi STA/AP and BLE.
// Callbacks only enqueue via proto_send_event (tx_task owns serial).
//
// Coexistence rules (ESP-IDF coex guide — do not "optimize"):
//  - Never esp_wifi_set_ps(WIFI_PS_NONE) with BT active; default WIFI_PS_MIN_MODEM keeps the arbiter stable.
//  - No manual esp_coex_*; the IDF SW arbiter slots the radio.
//  - Channel: a connected STA / live AP owns the channel and ESP-NOW inherits it;
//    only lock the requested channel when the radio is idle.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

#if BRIDGE_HAS_ESPNOW

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_mac.h>

static bool inited = false;

// Send-completion plumbing. Sends are serialized; TX callbacks arrive in order, so
// the counter drains fire-and-forget sends (emit SEND_EVT) before the blocking sync send (give sem).
static SemaphoreHandle_t tx_done_sem;
static volatile uint8_t ff_outstanding = 0;          // fire-and-forget sends in flight
static volatile uint8_t sync_status = 1;             // last sync result (0 = ACKed)

bool espnow_is_active() { return inited; }

static void on_tx_done(const uint8_t* dst_mac, esp_now_send_status_t status) {
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
  on_tx_done(info ? info->des_addr : nullptr, status);
}
#else
static void on_send(const uint8_t* mac, esp_now_send_status_t status) {
  on_tx_done(mac, status);
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

static uint8_t map_err(esp_err_t e) {
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
static void handle_init(uint8_t seq, uint16_t cmd, const uint8_t* p, uint16_t len) {
  if (len < 2) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
  uint8_t channel = p[0], flags = p[1];

  // ESP-NOW rides the Wi-Fi driver; bring up STA if off.
  if (WiFi.getMode() == WIFI_MODE_NULL) WiFi.mode(WIFI_STA);

  uint8_t proto = WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N;
  if (flags & 0x01) proto |= WIFI_PROTOCOL_LR;
  esp_wifi_set_protocol(WIFI_IF_STA, proto);

  // Associated STA / live AP owns the channel; only lock requested channel when idle.
  bool channel_owned = WiFi.status() == WL_CONNECTED || (WiFi.getMode() & WIFI_MODE_AP);
  if (!channel_owned) {
    esp_wifi_set_channel(channel ? channel : 1, WIFI_SECOND_CHAN_NONE);
  } else if (channel && channel != WiFi.channel()) {
    proto_log(1, "espnow: channel follows Wi-Fi while connected; request ignored");
  }

  if (!inited) {
    esp_err_t e = esp_now_init();
    if (e != ESP_OK) { proto_reply_err(seq, cmd, map_err(e)); return; }
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
static void handle_add_peer(uint8_t seq, uint16_t cmd, const uint8_t* p, uint16_t len) {
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
  if (e != ESP_OK) { proto_reply_err(seq, cmd, map_err(e)); return; }
  proto_reply_ok(seq, cmd);
}

// SEND: mac[6] | data (<= 250 bytes).
// seq != 0: block on TX callback, reply delivered u8 (1 = peer MAC ACKed; broadcasts never ACK).
// seq == 0: fire-and-forget; result arrives as ESPNOW_SEND_EVT.
static void handle_send(uint8_t seq, uint16_t cmd, const uint8_t* p, uint16_t len) {
  if (len < 6 || len > 6 + ESPNOW_MAX_DATA) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }

  if (seq == 0) {
    ff_outstanding++;
    if (esp_now_send(p, p + 6, len - 6) != ESP_OK) ff_outstanding--;
    return;  // no reply for fire-and-forget
  }

  while (xSemaphoreTake(tx_done_sem, 0) == pdTRUE) {}  // clear stale gives
  esp_err_t e = esp_now_send(p, p + 6, len - 6);
  if (e != ESP_OK) { proto_reply_err(seq, cmd, map_err(e)); return; }
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
      handle_init(seq, cmd, p, len);
      break;

    case 0x02:  // DEINIT (leave the Wi-Fi mode alone — STA/AP may be in use)
      esp_now_deinit();
      inited = false;
      ff_outstanding = 0;
      proto_reply_ok(seq, cmd);
      break;

    case 0x03:  // SET_PMK: pmk[16]
      if (len != 16) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (esp_now_set_pmk(p) != ESP_OK) { proto_reply_err(seq, cmd, ST_IO); return; }
      proto_reply_ok(seq, cmd);
      break;

    case 0x04:  // ADD_PEER
      handle_add_peer(seq, cmd, p, len);
      break;

    case 0x05: {  // DEL_PEER: mac[6]
      if (len != 6) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      esp_err_t e = esp_now_del_peer(p);
      if (e != ESP_OK) { proto_reply_err(seq, cmd, map_err(e)); return; }
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x06:  // SEND
      handle_send(seq, cmd, p, len);
      break;

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#else  // !BRIDGE_HAS_ESPNOW

bool espnow_is_active() { return false; }

UNSUPPORTED_STUB(espnow_handle, MOD_ESPNOW)

#endif  // BRIDGE_HAS_ESPNOW
