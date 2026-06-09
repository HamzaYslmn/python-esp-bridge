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

// Size of the RX stream buffer (host-to-board direction).
// rx_task drains this nearly continuously; the worst-case stall is a slow
// inline command handler such as an I2C scan (~80 ms ≈ about 4 KB at BLE
// throughput). The buffer is intentionally lean: on a classic ESP32 running
// Wi-Fi + Bluedroid + ESP-NOW, the heap margin is only a few KB, so every
// KB allocated here is a KB that cannot be used for the radio stack.
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

// Hands-off connection-parameter policy.
// Hard-won lesson from running Wi-Fi + BLE + ESP-NOW simultaneously:
// the peripheral must NOT initiate any link-layer control procedures after
// connecting — specifically no esp_ble_gap_update_conn_params and no
// esp_ble_gap_set_pkt_data_len.
//
// Issuing those requests immediately after connect races the central's own
// setup procedures. Windows in particular runs several setup steps
// (MTU exchange, service discovery, security) right at connect time, and if
// our parameter-update request collides with any of those, the Link Layer
// stalls: the connection appears "up", but ATT goes silent and the host
// times out partway through the handshake.
//
// The correct approach: the central owns the connection parameters. We
// publish our preferred connection interval passively in the advertisement
// payload via setMinPreferred/setMaxPreferred (the Slave Connection Interval
// Range AD type). A central that honours it applies fast parameters before
// connecting; one that ignores it still works, just at its default interval.
class LinkSrvCb : public BLEServerCallbacks {
  void onConnect(BLEServer*) override {
    connected = true;
    authed = false;       // reset auth on every new connection — the host must re-authenticate each time
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
void link_ble_set_authed(bool v) { authed = v; }
const char* link_ble_password() { return link_password; }
void* link_ble_server() { return server; }

void link_ble_write(const uint8_t* data, uint16_t len) {
  if (!enabled || !connected || tx_chr == nullptr) return;
  uint16_t chunk_max = att_mtu > 3 ? att_mtu - 3 : 20;
  uint8_t burst = 0;
  while (len > 0) {
    // Congestion-driven flow control: if Bluedroid's TX queue is full,
    // spin-wait for it to drain rather than sending a notification that
    // would be silently dropped. We cap the wait at 250 ms; if it is still
    // congested after that, something is seriously wrong and the host's
    // pending request will time out and retry anyway.
    for (uint16_t waited = 0; congested && connected && waited < 250; waited++)
      vTaskDelay(1);
    if (!connected) return;
    uint16_t n = len < chunk_max ? len : chunk_max;
    tx_chr->setValue((uint8_t*)data, n);
    tx_chr->notify();
    data += n;
    len -= n;
    if (++burst >= 8 && len > 0) {  // yield briefly every 8 chunks so RX and net tasks can run
      burst = 0;
      vTaskDelay(1);
    }
  }
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
void link_ble_write(const uint8_t*, uint16_t) {}
uint16_t link_ble_read(uint8_t*, uint16_t) { return 0; }
void* link_ble_server() { return nullptr; }

#endif
