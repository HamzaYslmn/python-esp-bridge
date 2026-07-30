#if defined(ARDUINO_ARCH_ESP32)
// BLE module: advertising/scanning, a GATT server (multiple services), and a
// basic GATT client supporting a single simultaneous connection.
// Uses the Bluedroid BLE library bundled with arduino-esp32 core 3.x.
//
// Command handlers run on slow_task. Bluedroid internal callbacks (scan results,
// GATT writes, connect/disconnect) run on BT stack tasks. Both paths only
// enqueue data via proto_send_event — tx_task is the sole serial writer.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/link.h"

#if BRIDGE_BLE

#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <BLEServer.h>
#include <BLEClient.h>
#include <BLE2902.h>

static bool ble_ready = false;
static BLEScan* scan_obj = nullptr;
static BLEServer* server_obj = nullptr;
static BLECharacteristic* chars[BLE_MAX_CHARS];
static uint8_t char_count = 0;
static BLEClient* client_obj = nullptr;

static void ble_lazy_init() {
  if (ble_ready) return;
  bt_prepare_ble_only();  // on classic ESP32: release Classic BT memory and restart the controller in BLE-only mode (see link_ble.cpp)
  BLEDevice::init(BRIDGE_NAME);
  ble_ready = true;
}

// ---- UUID helpers -----------------------------------------------------------
// All UUIDs on the wire are 16 bytes, big-endian (MSB first).
// Bluedroid stores 128-bit UUIDs in little-endian order internally, so
// uuid_to_wire() reverses the byte order when converting to the wire format.
// Short (16- or 32-bit) UUIDs are expanded into the standard Bluetooth base UUID.
static const uint8_t BASE_UUID_MSB[16] = {
  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00,
  0x80, 0x00, 0x00, 0x80, 0x5F, 0x9B, 0x34, 0xFB,
};

static void uuid_to_wire(BLEUUID uuid, uint8_t out[16]) {
  const esp_bt_uuid_t* n = uuid.getNative();
  if (n->len == ESP_UUID_LEN_128) {
    for (int i = 0; i < 16; i++) out[i] = n->uuid.uuid128[15 - i];  // LE -> MSB
  } else {
    memcpy(out, BASE_UUID_MSB, 16);
    uint32_t v = n->len == ESP_UUID_LEN_16 ? n->uuid.uuid16 : n->uuid.uuid32;
    write_be32(out, v);
  }
}

static BLEUUID wire_to_uuid(const uint8_t* w) {
  return BLEUUID((uint8_t*)w, 16, true /*msbFirst*/);
}

// ---- scan -------------------------------------------------------------------
static const uint8_t BLE_ADV_MAX_PAYLOAD = 62;  // max adv+scanrsp bytes we forward
class AdvCb : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice dev) override {
    uint8_t buf[8 + BLE_ADV_MAX_PAYLOAD];
    // getNative() returns a pointer to the raw 6-byte address array.
    // The type changed across arduino-esp32 versions; uint8_t* is correct on core 3.x.
    memcpy(buf, dev.getAddress().getNative(), 6);
    buf[6] = (uint8_t)dev.getAddressType();
    buf[7] = (uint8_t)(int8_t)dev.getRSSI();
    uint8_t plen = dev.getPayloadLength() > BLE_ADV_MAX_PAYLOAD ? BLE_ADV_MAX_PAYLOAD : dev.getPayloadLength();
    memcpy(buf + 8, dev.getPayload(), plen);
    proto_send_event(BLE_ADV_EVT, buf, 8 + plen);
  }
};
static AdvCb adv_cb;

// ---- GATT server callbacks --------------------------------------------------
class SrvCb : public BLEServerCallbacks {
  void onConnect(BLEServer*) override {
    uint8_t v = 1;
    proto_send_event(BLE_GATTS_CONN_EVT, &v, 1);
  }
  void onDisconnect(BLEServer* s) override {
    uint8_t v = 0;
    proto_send_event(BLE_GATTS_CONN_EVT, &v, 1);
    s->startAdvertising();  // restart advertising so new clients can find the device
  }
};
static SrvCb srv_cb;

class ChrCb : public BLECharacteristicCallbacks {
 public:
  uint8_t id;
  explicit ChrCb(uint8_t i) : id(i) {}
  void onWrite(BLECharacteristic* c) override {
    uint8_t buf[1 + 256];
    size_t n = c->getLength();
    if (n > 256) n = 256;
    buf[0] = id;
    memcpy(buf + 1, c->getData(), n);
    proto_send_event(BLE_GATTS_WR_EVT, buf, 1 + n);
  }
};

// ---- GATT client callbacks --------------------------------------------------
class CliCb : public BLEClientCallbacks {
  void onConnect(BLEClient*) override {}
  void onDisconnect(BLEClient*) override {
    proto_send_event(BLE_GATTC_DISC_EVT, nullptr, 0);
  }
};
static CliCb cli_cb;

static void notify_cb(BLERemoteCharacteristic* chr, uint8_t* data, size_t len, bool) {
  uint8_t buf[16 + 256];
  uuid_to_wire(chr->getUUID(), buf);
  if (len > 256) len = 256;
  memcpy(buf + 16, data, len);
  proto_send_event(BLE_GATTC_NTFY_EVT, buf, 16 + len);
}

static BLERemoteCharacteristic* find_remote_chr(const uint8_t* svc_w, const uint8_t* chr_w) {
  if (!client_obj || !client_obj->isConnected()) return nullptr;
  BLERemoteService* svc = client_obj->getService(wire_to_uuid(svc_w));
  if (!svc) return nullptr;
  return svc->getCharacteristic(wire_to_uuid(chr_w));
}

// SYS_RADIO_OFF asks before killing the BT stack: once this module has touched
// Bluedroid, deinit would leave its scan/server/client pointers dangling.
bool ble_module_active() { return ble_ready; }

void ble_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_BLE, op);
  if (link_bt_dead()) { proto_reply_err(seq, cmd, ST_UNSUPPORTED); return; }  // SYS_RADIO_OFF ran; reboot to get BLE back
  ble_lazy_init();

  switch (op) {
    case 0x01: {  // SCAN_START: duration_s (0=forever), active
      NEED(2);
      scan_obj = BLEDevice::getScan();
      scan_obj->setAdvertisedDeviceCallbacks(&adv_cb, true /*want duplicates*/);
      scan_obj->setActiveScan(p[1] != 0);
      scan_obj->setInterval(100);
      scan_obj->setWindow(99);
      if (!scan_obj->start(p[0], nullptr, false)) { proto_reply_err(seq, cmd, ST_IO); return; }
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x02:  // SCAN_STOP
      if (scan_obj) scan_obj->stop();
      proto_reply_ok(seq, cmd);
      break;

    case 0x03: {  // ADV_START: name, mfg data, svc_uuid16
      const uint8_t* q = p;
      uint16_t left = len;
      if (left < 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      uint8_t nlen = *q++; left--;
      if (left < nlen) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      String name((const char*)q, nlen); q += nlen; left -= nlen;
      if (left < 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      uint8_t mlen = *q++; left--;
      if (left < (uint16_t)(mlen + 2)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      String mfg((const char*)q, mlen); q += mlen; left -= mlen;
      uint16_t uuid16 = read_be16(q);

      BLEAdvertising* adv = BLEDevice::getAdvertising();
      BLEAdvertisementData data;
      if (nlen) data.setName(name);
      if (mlen) data.setManufacturerData(mfg);
      if (uuid16) data.setCompleteServices(BLEUUID(uuid16));
      data.setFlags(0x06);  // AD flags: LE general discoverable, BR/EDR (Classic BT) not supported
      adv->setAdvertisementData(data);
      adv->start();
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x04:  // ADV_STOP
      BLEDevice::getAdvertising()->stop();
      proto_reply_ok(seq, cmd);
      break;

    case 0x05: {  // GATTS_DEF: svc_uuid[16], n, {uuid[16], props}*n -> char ids
      NEED(17);
      uint8_t n = p[16];
      if (n == 0 || char_count + n > BLE_MAX_CHARS ||
          len < (uint16_t)(17 + n * 17)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (!server_obj) {
        // Bluedroid allows only one GATT server per device. If link_ble.cpp
        // has already created one for the bridge link, reuse it here instead
        // of creating a second one. In that case link_ble.cpp's connection
        // callbacks remain in charge of connect/disconnect events.
        server_obj = (BLEServer*)link_ble_server();
        if (!server_obj) {
          server_obj = BLEDevice::createServer();
          server_obj->setCallbacks(&srv_cb);
        }
      }
      BLEService* svc = server_obj->createService(wire_to_uuid(p));
      uint8_t ids[BLE_MAX_CHARS];
      for (uint8_t i = 0; i < n; i++) {
        const uint8_t* cdef = p + 17 + i * 17;
        uint8_t props = cdef[16];
        uint32_t blprops = 0;
        if (props & GATT_PROP_READ)     blprops |= BLECharacteristic::PROPERTY_READ;
        if (props & GATT_PROP_WRITE)    blprops |= BLECharacteristic::PROPERTY_WRITE;
        if (props & GATT_PROP_NOTIFY)   blprops |= BLECharacteristic::PROPERTY_NOTIFY;
        if (props & GATT_PROP_WRITE_NR) blprops |= BLECharacteristic::PROPERTY_WRITE_NR;
        BLECharacteristic* chr = svc->createCharacteristic(wire_to_uuid(cdef), blprops);
        if (props & GATT_PROP_NOTIFY) chr->addDescriptor(new BLE2902());
        chr->setCallbacks(new ChrCb(char_count));
        chars[char_count] = chr;
        ids[i] = char_count++;
      }
      svc->start();
      proto_reply(seq, cmd, ids, n);
      break;
    }

    case 0x06:    // GATTS_SET: char_id, data..
    case 0x07: {  // GATTS_NTFY: char_id, data..
      if (len < 1 || p[0] >= char_count) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      chars[p[0]]->setValue((uint8_t*)(p + 1), len - 1);
      if (op == 0x07) chars[p[0]]->notify();
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x08: {  // GATTC_CONN: addr[6], addr_type
      NEED(7);
      if (client_obj && client_obj->isConnected()) { proto_reply_err(seq, cmd, ST_BUSY); return; }
      if (!client_obj) {
        client_obj = BLEDevice::createClient();
        client_obj->setClientCallbacks(&cli_cb);
      }
      esp_bd_addr_t addr;
      memcpy(addr, p, 6);
      if (!client_obj->connect(BLEAddress(addr), (esp_ble_addr_type_t)p[6])) {
        proto_reply_err(seq, cmd, ST_IO);
        return;
      }
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x09:  // GATTC_DISC
      if (client_obj && client_obj->isConnected()) client_obj->disconnect();
      proto_reply_ok(seq, cmd);
      break;

    case 0x0A: {  // GATTC_READ: svc[16], chr[16] -> data
      NEED(32);
      BLERemoteCharacteristic* chr = find_remote_chr(p, p + 16);
      if (!chr) { proto_reply_err(seq, cmd, ST_IO); return; }
      String v = chr->readValue();
      proto_reply(seq, cmd, (const uint8_t*)v.c_str(), v.length());
      break;
    }

    case 0x0B: {  // GATTC_WRITE: svc[16], chr[16], data..
      NEED(32);
      BLERemoteCharacteristic* chr = find_remote_chr(p, p + 16);
      if (!chr) { proto_reply_err(seq, cmd, ST_IO); return; }
      chr->writeValue((uint8_t*)(p + 32), len - 32, true);
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x0C: {  // GATTC_SUB: svc[16], chr[16], enable
      NEED(33);
      BLERemoteCharacteristic* chr = find_remote_chr(p, p + 16);
      if (!chr) { proto_reply_err(seq, cmd, ST_IO); return; }
      chr->registerForNotify(p[32] ? notify_cb : nullptr);
      proto_reply_ok(seq, cmd);
      break;
    }

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#else  // !BRIDGE_BLE

bool ble_module_active() { return false; }

UNSUPPORTED_STUB(ble_handle, MOD_BLE)

#endif
#endif  // ARDUINO_ARCH_ESP32
