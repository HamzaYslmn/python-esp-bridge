#if defined(ARDUINO_ARCH_ESP32)
// BLE transport link: runs the bridge protocol over a NUS-style GATT service
// so a host can communicate with the board over Bluetooth without a USB cable.
// See espbridge/link.h for the public API.
//
// RX: the host writes the RX characteristic, Bluedroid's BT task pushes the bytes
//   into a stream buffer, and rx_task drains it through the same COBS pump the
//   USB serial path uses.
// TX: tx_task sends (ATT_MTU - 3)-byte chunks as GATT notifications.
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

// Classic ESP32 boots a dual-mode controller; this firmware uses only BLE, so
// we release the Classic BT memory pool and start the controller BLE-only before
// any BT init. Two reasons: (1) heap — Classic BT's reservation is heap that
// Bluedroid + Wi-Fi coex need; (2) stability — a full dual-mode init runs
// OBEX_Deinit on an unallocated control block, crashing core 0 at startup.
// One-way: released Classic BT memory needs a reboot to reclaim.
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

// RX stream buffer (host-to-board), sized at the host's 4300-byte in-flight cap
// plus one frame so it cannot overflow. Two max-size frames already saturate BLE
// — the central's per-connection-event write rate is the bottleneck, not
// in-flight count — and every KB here is one the radio stack loses.
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

// Conn-param policy: hands-off at connect, tune after auth. Forcing LL params at
// connect races the central's setup (MTU/discovery/security) and stalls the link
// ATT-silent, so we only advertise a preferred interval passively. After SYS_AUTH
// (setup provably done) we tune in a ladder, each step driven by the prior step's
// event. Centrals grant a range's MAXIMUM, so we widen one notch per rejection to
// find the floor: (1) 7.5, (2) 7.5-11.25, (3) 7.5-15 ms, then (4) DLE to 251 B.
static esp_bd_addr_t peer_bda;  // central's address, captured at connect
static volatile uint8_t tune_state = 0;  // 0 idle; 1..3 ladder step asked; 4 DLE/done
static const uint16_t tune_max_int[] = {0x06, 0x09, 0x0C};  // 7.5 / 11.25 / 15 ms

static void request_conn_params(uint16_t min_int, uint16_t max_int,
                                uint16_t latency = 0, uint16_t timeout = 100) {  // timeout default 1 s
  esp_ble_conn_update_params_t p = {};
  memcpy(p.bda, peer_bda, sizeof(p.bda));
  p.min_int = min_int;
  p.max_int = max_int;
  p.latency = latency;
  p.timeout = timeout;  // supervision timeout (10 ms units): drop-detection delay before re-advertising
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
    // A Bluedroid BT task, not an ISR, so the plain stream-buffer send is right.
    // The 50 ms timeout lets a pipelined burst backpressure that task instead of
    // dropping bytes; still full after 50 ms means the host will retry anyway.
    size_t n = xStreamBufferSend(rx_buf, c->getData(), c->getLength(),
                                 pdMS_TO_TICKS(50));
    if (n < c->getLength()) {
      // Never drop silently: a partial COBS frame looks like a random host-side
      // timeout. Count it (SYS_FREE_HEAP) and emit a rate-limited warning.
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

// Congestion handler: on ESP_GATTS_CONGEST_EVT (Bluedroid TX queue full), set the
// `congested` flag so link_ble_write_chunk pauses — notifications sent into a full
// queue are silently dropped, causing mysterious timeouts when pipelining.
static void link_gatts_evt(esp_gatts_cb_event_t event, esp_gatt_if_t,
                           esp_ble_gatts_cb_param_t* param) {
  if (event == ESP_GATTS_CONGEST_EVT) congested = param->congest.congested;
}

// GAP watch: advances the post-auth tuning ladder (tune_state) and logs each
// outcome, so a central rejecting fast params is visible, not just "BLE feels slow".
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

// Min free heap before BLEDevice::init(): Bluedroid + the GATT service need this
// much atop the controller, and below it init() aborts in operator new with no
// recoverable path. Checked explicitly so BLE is skipped, not fatal.
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

  // Advertised name "espbridge_<identity>": the prefix marks the board as ours
  // in any BLE scanner, and the identity is its name, or its MAC while unnamed —
  // whichever one you'd pass to Bridge(). The scan response fits ~26 characters
  // and the prefix spends 10, so only one identity fits: BRIDGE_NAME_MAX (16) is
  // what keeps a named board's advert whole.
  const char* custom = sys_device_name();
  char devname[10 + BRIDGE_NAME_MAX + 1];
  if (custom[0]) {
    snprintf(devname, sizeof(devname), "espbridge_%s", custom);
  } else {
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(devname, sizeof(devname), "espbridge_%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  }
  BLEDevice::init(devname);
  // Max TX power (adv + connections): fewer retransmits, more range, for ~3 mA
  // — a fine trade on a wired-powered board.
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
  // Advertise the preferred conn interval (Slave Conn Interval Range AD field) so
  // centrals apply the fast interval from connect, no update request needed.
  adv->setMinPreferred(0x06);  // 7.5 ms
  adv->setMaxPreferred(0x0C);  // 15 ms
  adv->start();
  enabled = true;
  proto_log_heap("ble: link up");
}

bool link_ble_enabled() { return enabled; }
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

bool link_ble_power(bool battery) {
  if (!link_ble_up()) return false;
  if (battery) {
    // 50-100 ms interval + slave latency 4 (latency only skips events when
    // the board has nothing to send, so a busy link stays responsive).
    tune_state = 4;  // park the fast ladder so GAP events don't re-tighten
    request_conn_params(0x28, 0x50, 4, 400);  // 4 s timeout: spec floor here is (1+4)*100*2 = 1 s
  } else {
    tune_state = 1;  // re-run the fast ladder from the 7.5 ms spec minimum
    request_conn_params(0x06, tune_max_int[0]);
  }
  return true;
}

bool link_ble_up() { return enabled && connected && tx_chr != nullptr; }

// Congestion-driven flow control: while congested, a notification would be
// dropped, so tx_task skips this link until it clears (vs. spin-waiting 250 ms,
// which stalled the serial link behind it).
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

// SYS_RADIO_OFF: tear the whole BT stack down — advertising, Bluedroid, the
// controller — and release its memory to the heap. Removes the stack's core-0
// interrupt/scheduler load for jitter-sensitive work over USB and frees
// ~60 KB. One-way: released controller memory needs a reboot to reclaim, so
// Bluetooth stays off until reset. Runs on slow_task (next to every other
// Bluedroid caller); refused while a central holds the link.
static bool bt_dead = false;

bool link_ble_shutdown() {
  if (connected) return false;  // a BLE central would saw off its own branch
  enabled = false;
  bt_dead = true;
  tx_chr = nullptr;             // tx_task checks link_ble_up() before touching it
  BLEDevice::deinit(true);      // no-op if init was skipped (boot heap guard)...
  if (btStarted()) btStop();    // ...but the bare controller may still be up
  proto_log_heap("ble: bt stack off");
  return true;
}

bool link_bt_dead() { return bt_dead; }

#else  // !BRIDGE_BLE — empty stubs so the rest of the codebase links cleanly on BLE-less builds

void link_ble_init(const char*) {}
void bt_prepare_ble_only() {}
bool link_ble_enabled() { return false; }
uint32_t link_ble_rx_dropped() { return 0; }
bool link_ble_authed() { return false; }
void link_ble_set_authed(bool) {}
const char* link_ble_password() { return ""; }
bool link_ble_up() { return false; }
bool link_ble_power(bool) { return false; }
bool link_ble_writable() { return false; }
uint16_t link_ble_write_chunk(const uint8_t*, uint16_t) { return 0; }
uint16_t link_ble_read(uint8_t*, uint16_t) { return 0; }
void* link_ble_server() { return nullptr; }
bool link_ble_shutdown() { return false; }
bool link_bt_dead() { return false; }

#endif
#endif  // ARDUINO_ARCH_ESP32
