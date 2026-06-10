// BLE transport link: runs the bridge protocol over a NUS-style GATT service
// so a host can communicate with the board over Bluetooth without a USB cable.
// See espbridge/link.h for the public API.
//
// RX path: the host writes to the RX characteristic; Bluedroid's BT task
//   invokes the onWrite callback, which pushes bytes into a FreeRTOS stream
//   buffer. rx_task drains that buffer through the same COBS framing pump
//   used by the USB serial path.
// TX path: tx_task calls link_ble_write(), which sends the data in chunks
//   no larger than (ATT_MTU - 3) bytes via GATT notifications.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/link.h"

#if BRIDGE_BLE

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLE2902.h>
#include <esp_mac.h>
#include <esp_bt.h>
#include <esp_gap_ble_api.h>
#include <esp32-hal-bt.h>
#include <freertos/stream_buffer.h>

// The classic ESP32 ships with a dual-mode (Classic BT + BLE) controller,
// and the default Arduino sdkconfig keeps both modes enabled. This firmware
// only ever uses BLE, so we release the Classic BT memory pool back to the
// heap before any BT stack init. We also start the controller in BLE-only
// mode. This is important for two reasons:
//
//  1. Heap: Classic BT reserves a substantial chunk that Bluedroid + Wi-Fi
//     need on this chip family (every KB matters with coex active).
//
//  2. Stability: if we let Bluedroid do a full dual-mode init, BTE_InitStack
//     calls OBEX_Deinit, which dereferences a control block that was never
//     allocated in BLE-only builds — causing a crash on core 0 at startup.
//
// This is a one-way operation: once released, Classic BT memory cannot be
// reclaimed without a reboot. Nothing in this firmware uses Classic BT.
void bt_prepare_ble_only() {
#if defined(CONFIG_IDF_TARGET_ESP32)
  static bool done = false;
  if (done) return;
  done = true;
  esp_bt_mem_release(ESP_BT_MODE_CLASSIC_BT);
  // After btStartMode(BT_MODE_BLE), BLEDevice::init() detects the controller
  // already running and skips its own btStart() call. This matters because
  // btStart() would ask for dual-mode and fail after the Classic BT memory
  // has been released.
  btStartMode(BT_MODE_BLE);
#endif
}

// RX stream buffer (host-to-board). The Python side caps in-flight bytes at
// 4300 — two max-size wire frames, which already saturates BLE (the central's
// per-connection-event write rate is the bottleneck, not in-flight count) —
// and the ring holds that cap plus one more frame, so it can't overflow.
// Kept lean: on a classic ESP32 with Wi-Fi + Bluedroid + ESP-NOW every KB
// here is one the radio stack loses.
#define LINK_RX_BUF 6400

static bool enabled = false;
static volatile bool connected = false;
static volatile bool authed = false;
static volatile bool congested = false;  // BT stack TX queue full
static volatile uint16_t att_mtu = 23;
static const char* link_password = "";
static BLEServer* server = nullptr;
static BLECharacteristic* tx_chr = nullptr;
static StreamBufferHandle_t rx_buf = nullptr;
static volatile uint32_t rx_dropped = 0;  // bytes lost to RX buffer overflow

// Connection-parameter policy: hands-off at connect, tune after auth.
// Initiating LL control procedures right after connecting races the central's
// own setup (Windows runs MTU exchange / discovery / security then), stalling
// the Link Layer — "up" but ATT-silent until the host times out. So at connect
// we only advertise a preferred interval passively (setMin/MaxPreferred);
// Windows ignores it and sits at ~20 ms.
// After SYS_AUTH — the last handshake step, so connect-time setup is provably
// done — we tune in a strict ladder, each step kicked off by the previous
// one's completion event. Centrals that accept a range grant its MAXIMUM, so
// the ladder widens the range one notch per rejection to find the central's
// true floor: (1) 7.5 ms, (2) 7.5-11.25 ms, (3) 7.5-15 ms, then (4) data
// length extension to 251-byte PDUs. RTT = 2x the granted interval.
static esp_bd_addr_t peer_bda;  // central's address, captured at connect
static volatile uint8_t tune_state = 0;  // 0 idle; 1..3 ladder step asked; 4 DLE/done
static const uint16_t tune_max_int[] = {0x06, 0x09, 0x0C};  // 7.5 / 11.25 / 15 ms

static void request_conn_params(uint16_t min_int, uint16_t max_int) {
  esp_ble_conn_update_params_t p = {};
  memcpy(p.bda, peer_bda, sizeof(p.bda));
  p.min_int = min_int;
  p.max_int = max_int;
  p.latency = 0;
  p.timeout = 400;  // 4 s supervision timeout
  esp_err_t err = esp_ble_gap_update_conn_params(&p);
  if (err != ESP_OK) {
    char msg[64];
    snprintf(msg, sizeof(msg), "ble: conn param request failed (%d)", (int)err);
    proto_log(2, msg);
  }
}

class LinkSrvCb : public BLEServerCallbacks {
  void onConnect(BLEServer*) override {
    connected = true;
    authed = false;       // reset auth on every new connection — the host must re-authenticate each time
    att_mtu = 23;
    congested = false;
  }
  void onConnect(BLEServer*, esp_ble_gatts_cb_param_t* param) override {
    memcpy(peer_bda, param->connect.remote_bda, sizeof(peer_bda));
    tune_state = 0;
  }
  void onDisconnect(BLEServer* s) override {
    connected = false;
    authed = false;
    congested = false;
    if (rx_buf) xStreamBufferReset(rx_buf);
    s->startAdvertising();  // stay discoverable
  }
  void onMtuChanged(BLEServer*, esp_ble_gatts_cb_param_t* param) override {
    att_mtu = param->mtu.mtu;
  }
};
static LinkSrvCb link_srv_cb;

class LinkRxCb : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* c) override {
    if (rx_buf == nullptr) return;
    // This callback runs on a Bluedroid BT task (not an ISR), so the
    // plain (non-ISR) stream buffer send is correct here.
    // We allow a short blocking timeout (50 ms) so that pipelined write
    // bursts naturally backpressure the BT task instead of dropping bytes.
    // If the buffer is still full after 50 ms, something is very wrong;
    // at that point the host will time out and retry anyway.
    size_t n = xStreamBufferSend(rx_buf, c->getData(), c->getLength(),
                                 pdMS_TO_TICKS(50));
    if (n < c->getLength()) {
      // Do not drop bytes silently. If we discard part of a COBS frame
      // the rx_task sees a corrupt frame, which looks like a random timeout
      // or garbled response on the host — extremely hard to diagnose.
      // Instead: increment rx_dropped (visible via SYS_FREE_HEAP) and
      // emit a rate-limited warning so the problem is surfaced clearly.
      rx_dropped += c->getLength() - n;
      static uint32_t last_warn = 0;
      uint32_t now = millis();
      if (now - last_warn > 1000) {
        last_warn = now;
        proto_log(2, "ble: rx overflow — frames lost (host writing faster "
                     "than commands execute; update python-esp-bridge for "
                     "burst throttling)");
      }
    }
  }
};
static LinkRxCb link_rx_cb;

// Congestion handler: Bluedroid raises ESP_GATTS_CONGEST_EVT when its TX queue
// fills up. We set the `congested` flag and pause notifications in
// link_ble_write() until it clears. Without this, notifications sent into a
// full queue are silently discarded, causing mysterious host timeouts when
// the board is pipelining responses.
static void link_gatts_evt(esp_gatts_cb_event_t event, esp_gatt_if_t,
                           esp_ble_gatts_cb_param_t* param) {
  if (event == ESP_GATTS_CONGEST_EVT) congested = param->congest.congested;
}

// GAP watch: advances the post-auth tuning sequence (see tune_state) and logs
// each outcome, so a central rejecting fast parameters is visible instead of
// just "BLE feels slow". tune_state stops the central's own later updates from
// re-triggering the sequence.
static void link_gap_evt(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t* param) {
  if (event == ESP_GAP_BLE_UPDATE_CONN_PARAMS_EVT) {
    char msg[96];
    snprintf(msg, sizeof(msg),
             "ble: conn params status=%d interval=%.2fms latency=%d timeout=%dms",
             (int)param->update_conn_params.status,
             param->update_conn_params.conn_int * 1.25,
             (int)param->update_conn_params.latency,
             (int)param->update_conn_params.timeout * 10);
    proto_log(1, msg);
    if (tune_state >= 1 && tune_state <= 2 && param->update_conn_params.status != 0) {
      tune_state++;  // rejected: widen the range one notch and retry
      request_conn_params(0x06, tune_max_int[tune_state - 1]);
    } else if (tune_state >= 1 && tune_state <= 3) {
      tune_state = 4;  // parameters settled: now (and only now) ask for DLE
      esp_ble_gap_set_pkt_data_len(peer_bda, 251);
    }
  } else if (event == ESP_GAP_BLE_SET_PKT_LENGTH_COMPLETE_EVT) {
    char msg[80];
    snprintf(msg, sizeof(msg), "ble: data len status=%d rx=%d tx=%d",
             (int)param->pkt_data_length_cmpl.status,
             (int)param->pkt_data_length_cmpl.params.rx_len,
             (int)param->pkt_data_length_cmpl.params.tx_len);
    proto_log(1, msg);
  }
}

// Minimum free heap required before calling BLEDevice::init().
// Bluedroid's host stack and the GATT service together need roughly this much
// on top of the already-started BLE controller. Below this threshold,
// BLEDevice::init() will fail deep inside operator new (bad_alloc -> abort)
// with no recoverable error path.
// We check the heap explicitly so we can fail gracefully: skip the BLE link,
// log a clear reason, and leave USB serial + Wi-Fi + ESP-NOW fully functional.
#define BLE_MIN_FREE_HEAP 55000

void link_ble_init(const char* password) {
  bt_prepare_ble_only();
  proto_log_heap("ble: controller up");
  if (ESP.getFreeHeap() < BLE_MIN_FREE_HEAP) {
    proto_log(2, "ble: link disabled, not enough free heap at boot");
    return;
  }
  link_password = password ? password : "";
  rx_buf = xStreamBufferCreate(LINK_RX_BUF, 1);
  if (rx_buf == nullptr) return;

  // Build the BLE device name as espbridge_<mac>[_<custom name>].
  // This matches what SYS_INFO reports, so a host can identify and address
  // a specific board from scan results alone, without needing to connect first.
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  char devname[10 + 12 + 1 + BRIDGE_NAME_MAX + 1];
  int n = snprintf(devname, sizeof(devname), "espbridge_%02x%02x%02x%02x%02x%02x",
                   mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  const char* custom = sys_device_name();
  if (custom[0]) snprintf(devname + n, sizeof(devname) - n, "_%s", custom);
  BLEDevice::init(devname);
  // Set maximum TX power on all power categories (advertising, connections,
  // default). A stronger signal means fewer link-layer retransmits, which
  // gives a more stable link and greater range. The cost is roughly 3 mA of
  // additional current draw — an acceptable trade-off for a wired-powered
  // bridge board.
  BLEDevice::setPower(ESP_PWR_LVL_P9, ESP_BLE_PWR_TYPE_DEFAULT);
  BLEDevice::setPower(ESP_PWR_LVL_P9, ESP_BLE_PWR_TYPE_ADV);
  BLEDevice::setMTU(517);
  BLEDevice::setCustomGattsHandler(link_gatts_evt);  // congestion watch
  BLEDevice::setCustomGapHandler(link_gap_evt);      // conn-param outcome watch

  server = BLEDevice::createServer();
  server->setCallbacks(&link_srv_cb);

  BLEService* svc = server->createService(BLE_LINK_SERVICE_UUID);
  BLECharacteristic* rx = svc->createCharacteristic(
      BLE_LINK_RX_UUID,
      BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  rx->setCallbacks(&link_rx_cb);
  tx_chr = svc->createCharacteristic(BLE_LINK_TX_UUID,
                                     BLECharacteristic::PROPERTY_NOTIFY);
  tx_chr->addDescriptor(new BLE2902());
  svc->start();

  BLEAdvertising* adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(BLE_LINK_SERVICE_UUID);
  adv->setScanResponse(true);  // put the full device name in the scan response (keeps the main ADV packet short)
  // Advertise our preferred connection interval as the "Slave Connection
  // Interval Range" AD field. Centrals read this from the advertisement
  // before connecting, so they can apply the fast interval from the start —
  // avoiding the need for a connection-parameter update request (which we
  // deliberately never send; see the hands-off policy note above).
  adv->setMinPreferred(0x06);  // 7.5 ms
  adv->setMaxPreferred(0x0C);  // 15 ms
  adv->start();
  enabled = true;
  proto_log_heap("ble: link up");
}

bool link_ble_enabled() { return enabled; }
bool link_ble_connected() { return connected; }
uint32_t link_ble_rx_dropped() { return rx_dropped; }
bool link_ble_authed() { return connected && authed; }

void link_ble_set_authed(bool v) {
  authed = v;
  if (!v || !connected || tune_state != 0) return;
  // Kick off post-auth link tuning (see tune_state); link_gap_evt() drives the
  // rest. The central resolves each request async; rejection leaves defaults.
  tune_state = 1;
  request_conn_params(0x06, tune_max_int[0]);  // start at the spec minimum, 7.5 ms
}
const char* link_ble_password() { return link_password; }
void* link_ble_server() { return server; }

bool link_ble_up() { return enabled && connected && tx_chr != nullptr; }

// Congestion-driven flow control: when Bluedroid's TX queue is full
// (ESP_GATTS_CONGEST_EVT), a notification would be silently discarded —
// so tx_task just skips this link until the congestion clears, instead of
// the old behaviour of spin-waiting up to 250 ms (which stalled the serial
// link's frames behind it).
bool link_ble_writable() { return link_ble_up() && !congested; }

uint16_t link_ble_write_chunk(const uint8_t* data, uint16_t len) {
  if (!link_ble_writable() || len == 0) return 0;
  uint16_t chunk = att_mtu > 3 ? att_mtu - 3 : 20;
  if (len < chunk) chunk = len;
  tx_chr->setValue((uint8_t*)data, chunk);
  tx_chr->notify();
  return chunk;
}

uint16_t link_ble_read(uint8_t* buf, uint16_t maxlen) {
  if (!enabled || rx_buf == nullptr) return 0;
  return (uint16_t)xStreamBufferReceive(rx_buf, buf, maxlen, 0);
}

#else  // !BRIDGE_BLE — empty stubs so the rest of the codebase links cleanly on BLE-less builds

void link_ble_init(const char*) {}
void bt_prepare_ble_only() {}
bool link_ble_enabled() { return false; }
bool link_ble_connected() { return false; }
uint32_t link_ble_rx_dropped() { return 0; }
bool link_ble_authed() { return false; }
void link_ble_set_authed(bool) {}
const char* link_ble_password() { return ""; }
bool link_ble_up() { return false; }
bool link_ble_writable() { return false; }
uint16_t link_ble_write_chunk(const uint8_t*, uint16_t) { return 0; }
uint16_t link_ble_read(uint8_t*, uint16_t) { return 0; }
void* link_ble_server() { return nullptr; }

#endif
