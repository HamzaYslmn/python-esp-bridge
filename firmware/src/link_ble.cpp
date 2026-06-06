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
#include <esp_mac.h>
#include <esp_bt.h>
#include <esp32-hal-bt.h>
#include <freertos/stream_buffer.h>

// Classic ESP32 ships a dual-mode (Classic BT + BLE) controller and the
// Arduino sdkconfig keeps it that way, but this firmware is BLE-only.
// Hand the Classic-BT memory back to the heap BEFORE any BT init and start
// the controller in BLE-only mode: with the Wi-Fi driver already up for
// coexistence (BRIDGE_WIFI_COEX) there otherwise isn't enough heap for
// Bluedroid, and its failed init crashes on core 0 (BTE_InitStack error
// path -> OBEX_Deinit dereferences a never-allocated control block).
// One-way until reboot; nothing in this firmware uses Classic BT.
void bt_prepare_ble_only() {
#if defined(CONFIG_IDF_TARGET_ESP32)
  static bool done = false;
  if (done) return;
  done = true;
  esp_bt_mem_release(ESP_BT_MODE_CLASSIC_BT);
  // BLEDevice::init() then sees the controller ENABLED and skips its own
  // btStart() (which would ask for dual mode and fail post-release).
  btStartMode(BT_MODE_BLE);
#endif
}

// Buffers host->board BLE writes until rx_task drains them (~continuous; the
// worst stall is a slow inline handler like an I2C scan, ~80 ms ≈ 4 KB at BLE
// speed). Kept lean: classic-ESP32 coex (Wi-Fi + Bluedroid + ESP-NOW) runs
// within a few KB of the heap limit, so every KB here matters.
#define LINK_RX_BUF 6144

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

// Hands-off link policy (lesson from Esp-WiFi-BLE-Now, which runs all three
// radios stably): the peripheral issues NO link-layer control procedures —
// no esp_ble_gap_update_conn_params, no esp_ble_gap_set_pkt_data_len. Firing
// those right at connect time races the central's own setup procedures
// (Windows especially) and a collision stalls the LL: the link stays "up"
// but ATT goes silent and the host times out mid-handshake. The central
// owns the connection parameters; our preferred interval is advertised
// passively in the AD payload (setMinPreferred below) where the central
// reads it BEFORE connecting — fast when honored, harmless when not.
class LinkSrvCb : public BLEServerCallbacks {
  void onConnect(BLEServer*) override {
    connected = true;
    authed = false;       // every connection starts unauthenticated
    att_mtu = 23;
    congested = false;
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
    // Bluedroid callbacks run on a BT task: plain (non-ISR) send is fine.
    // Allow a short block so pipelined bursts backpressure the BT task
    // instead of dropping bytes; past that the host times out & retries.
    size_t n = xStreamBufferSend(rx_buf, c->getData(), c->getLength(),
                                 pdMS_TO_TICKS(50));
    if (n < c->getLength()) {
      // Never drop silently — corrupted frames look like random timeouts on
      // the host. Count (SYS_FREE_HEAP reports it) and warn, rate-limited.
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

// Bluedroid raises CONGEST when its TX queue fills; pause notifies until it
// clears instead of silently losing them (= host timeouts under pipelining).
static void link_gatts_evt(esp_gatts_cb_event_t event, esp_gatt_if_t,
                           esp_ble_gatts_cb_param_t* param) {
  if (event == ESP_GATTS_CONGEST_EVT) congested = param->congest.congested;
}

// Bluedroid host + GATT service need roughly this much heap on top of the
// already-started controller; below it BLEDevice::init dies in operator new
// (bad_alloc -> abort) with no way to catch it. Fail soft instead: skip the
// BLE link, log why, and leave USB + Wi-Fi + ESP-NOW fully functional.
#define BLE_MIN_FREE_HEAP 55000

void link_ble_init(const char* password) {
  bt_prepare_ble_only();
  proto_log_heap("ble: controller up");
  if (ESP.getFreeHeap() < BLE_MIN_FREE_HEAP) {
    proto_log(2, "ble: link disabled, not enough free heap "
                 "(set BRIDGE_WIFI_COEX 0 if Wi-Fi/ESP-NOW is unused)");
    return;
  }
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
  // Max TX power on every power type: stronger signal = fewer link-layer
  // retransmits = a steadier link (and more range), for ~3 mA extra.
  BLEDevice::setPower(ESP_PWR_LVL_P9, ESP_BLE_PWR_TYPE_DEFAULT);
  BLEDevice::setPower(ESP_PWR_LVL_P9, ESP_BLE_PWR_TYPE_ADV);
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
  proto_log_heap("ble: link up");
}

bool link_ble_enabled() { return enabled; }
bool link_ble_connected() { return connected; }
uint32_t link_ble_rx_dropped() { return rx_dropped; }
bool link_ble_authed() { return connected && authed; }
void link_ble_set_authed(bool v) { authed = v; }
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
void bt_prepare_ble_only() {}
bool link_ble_enabled() { return false; }
bool link_ble_connected() { return false; }
uint32_t link_ble_rx_dropped() { return 0; }
bool link_ble_authed() { return false; }
void link_ble_set_authed(bool) {}
const char* link_ble_password() { return ""; }
void link_ble_write(const uint8_t*, uint16_t) {}
uint16_t link_ble_read(uint8_t*, uint16_t) { return 0; }
void* link_ble_server() { return nullptr; }

#endif
