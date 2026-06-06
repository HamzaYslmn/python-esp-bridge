// BLE link: carries the bridge protocol over a NUS-style GATT service so a
// host can talk to the board with no USB cable (see espbridge/link.h).
//
// RX: GATT write callbacks (BT task) push bytes into a FreeRTOS stream
// buffer; rx_task drains it through the same COBS pump as the serial port.
// TX: tx_task calls link_ble_write(), which notifies in <=MTU chunks.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/link.h"

#if BRIDGE_BLE

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLE2902.h>
#include <esp_gap_ble_api.h>
#include <esp_mac.h>
#include <freertos/stream_buffer.h>

// Big enough for several MAX_PAYLOAD frames in flight (pipelined hosts).
#define LINK_RX_BUF 16384

static bool enabled = false;
static volatile bool connected = false;
static volatile bool authed = false;
static volatile bool congested = false;  // BT stack TX queue full
static volatile uint16_t att_mtu = 23;
static const char* link_password = "";
static BLEServer* server = nullptr;
static BLECharacteristic* tx_chr = nullptr;
static StreamBufferHandle_t rx_buf = nullptr;

static esp_bd_addr_t peer_bda;
static volatile bool have_peer = false;

// Ask for a fast connection: 7.5-15 ms interval instead of the host's
// default 30-50 ms. Centrals (Windows especially) silently discard this
// while still busy with their own connection setup, so we ask repeatedly:
// on connect, after the MTU exchange, and once the client authenticates.
static void request_fast_conn() {
  if (!have_peer) return;
  esp_ble_conn_update_params_t cp = {};
  memcpy(cp.bda, peer_bda, sizeof(cp.bda));
  cp.min_int = 6;    // 6 * 1.25 ms = 7.5 ms
  cp.max_int = 12;   // 15 ms
  cp.latency = 0;
  cp.timeout = 400;  // 4 s supervision
  esp_ble_gap_update_conn_params(&cp);
}

class LinkSrvCb : public BLEServerCallbacks {
  void onConnect(BLEServer*, esp_ble_gatts_cb_param_t* param) override {
    connected = true;
    authed = false;       // every connection starts unauthenticated
    att_mtu = 23;
    congested = false;
    memcpy(peer_bda, param->connect.remote_bda, sizeof(peer_bda));
    have_peer = true;
    request_fast_conn();
    // Data length extension (BLE 4.2): 251-byte link packets instead of 27,
    // so a full ATT_MTU notification no longer fragments ~9x.
    esp_ble_gap_set_pkt_data_len(peer_bda, 251);
  }
  void onDisconnect(BLEServer* s) override {
    connected = false;
    authed = false;
    congested = false;
    have_peer = false;
    if (rx_buf) xStreamBufferReset(rx_buf);
    s->startAdvertising();  // stay discoverable
  }
  void onMtuChanged(BLEServer*, esp_ble_gatts_cb_param_t* param) override {
    att_mtu = param->mtu.mtu;
    request_fast_conn();  // central's setup is mostly done by now
  }
};
static LinkSrvCb link_srv_cb;

class LinkRxCb : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* c) override {
    if (rx_buf == nullptr) return;
    // Bluedroid callbacks run on a BT task: plain (non-ISR) send is fine.
    // Allow a short block so pipelined bursts backpressure the BT task
    // instead of dropping bytes; past that the host times out & retries.
    xStreamBufferSend(rx_buf, c->getData(), c->getLength(), pdMS_TO_TICKS(50));
  }
};
static LinkRxCb link_rx_cb;

// Bluedroid raises CONGEST when its TX queue fills; pause notifies until it
// clears instead of silently losing them (= host timeouts under pipelining).
static void link_gatts_evt(esp_gatts_cb_event_t event, esp_gatt_if_t,
                           esp_ble_gatts_cb_param_t* param) {
  if (event == ESP_GATTS_CONGEST_EVT) congested = param->congest.congested;
}

void link_ble_init(const char* password) {
  link_password = password ? password : "";
  rx_buf = xStreamBufferCreate(LINK_RX_BUF, 1);
  if (rx_buf == nullptr) return;

  // Advertise as espbridge_<mac>[_<custom name>] (same MAC/name SYS_INFO
  // reports) so hosts can discover, display and address every bridge
  // without connecting first.
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  char devname[10 + 12 + 1 + BRIDGE_NAME_MAX + 1];
  int n = snprintf(devname, sizeof(devname), "espbridge_%02x%02x%02x%02x%02x%02x",
                   mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  const char* custom = sys_device_name();
  if (custom[0]) snprintf(devname + n, sizeof(devname) - n, "_%s", custom);
  BLEDevice::init(devname);
  BLEDevice::setMTU(517);
  BLEDevice::setCustomGattsHandler(link_gatts_evt);  // congestion watch

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
  adv->setScanResponse(true);  // name goes in the scan response
  // Slave Connection Interval Range AD field: centrals read this at connect
  // time — before they would even see (let alone ignore) an update request.
  adv->setMinPreferred(0x06);  // 7.5 ms
  adv->setMaxPreferred(0x0C);  // 15 ms
  adv->start();
  enabled = true;
}

bool link_ble_enabled() { return enabled; }
bool link_ble_connected() { return connected; }
bool link_ble_authed() { return connected && authed; }
void link_ble_set_authed(bool v) {
  authed = v;
  if (v) request_fast_conn();  // setup is definitely over once auth lands
}
const char* link_ble_password() { return link_password; }
void* link_ble_server() { return server; }

void link_ble_write(const uint8_t* data, uint16_t len) {
  if (!enabled || !connected || tx_chr == nullptr) return;
  uint16_t chunk_max = att_mtu > 3 ? att_mtu - 3 : 20;
  uint8_t burst = 0;
  while (len > 0) {
    // Congestion-driven flow control: wait for the BT stack to drain
    // rather than dropping notifications (max ~250 ms, then give up —
    // the host's request will time out and it can retry).
    for (uint16_t waited = 0; congested && connected && waited < 250; waited++)
      vTaskDelay(1);
    if (!connected) return;
    uint16_t n = len < chunk_max ? len : chunk_max;
    tx_chr->setValue((uint8_t*)data, n);
    tx_chr->notify();
    data += n;
    len -= n;
    if (++burst >= 4 && len > 0) {  // brief yield on long frames
      burst = 0;
      vTaskDelay(1);
    }
  }
}

uint16_t link_ble_read(uint8_t* buf, uint16_t maxlen) {
  if (!enabled || rx_buf == nullptr) return 0;
  return (uint16_t)xStreamBufferReceive(rx_buf, buf, maxlen, 0);
}

#else  // !BRIDGE_BLE — stubs so protocol.cpp links on BLE-less builds

void link_ble_init(const char*) {}
bool link_ble_enabled() { return false; }
bool link_ble_connected() { return false; }
bool link_ble_authed() { return false; }
void link_ble_set_authed(bool) {}
const char* link_ble_password() { return ""; }
void link_ble_write(const uint8_t*, uint16_t) {}
uint16_t link_ble_read(uint8_t*, uint16_t) { return 0; }
void* link_ble_server() { return nullptr; }

#endif
