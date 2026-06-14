// BLE module (nRF52): central-side scanning. Counterpart to src/esp/mod_ble.cpp.
//
// Scope note: on this build the bridge's BLE *transport link* (link_ble.cpp)
// owns the peripheral role — advertising and the GATT server. A user GATT
// server / custom advertising (ADV_START, GATTS_*) would collide with it, and
// Bluefruit's GATT *client* uses statically pre-declared service/characteristic
// objects, which does not fit this protocol's dynamic per-request UUID model.
// So only scanning (a GAP observer, which runs fine alongside the link) is
// implemented; the GATT server/client ops reply ST_UNSUPPORTED.
#if defined(ARDUINO_ARCH_NRF52)
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/link.h"

#if BRIDGE_BLE

#include <bluefruit.h>

static const uint8_t BLE_ADV_MAX_PAYLOAD = 62;
static bool scanning = false;

// Runs on the SoftDevice event task. proto_send_event is thread-safe.
static void scan_cb(ble_gap_evt_adv_report_t* report) {
  uint8_t buf[8 + BLE_ADV_MAX_PAYLOAD];
  memcpy(buf, report->peer_addr.addr, 6);
  buf[6] = report->peer_addr.addr_type;
  buf[7] = (uint8_t)(int8_t)report->rssi;
  uint8_t plen = report->data.len > BLE_ADV_MAX_PAYLOAD ? BLE_ADV_MAX_PAYLOAD : report->data.len;
  memcpy(buf + 8, report->data.p_data, plen);
  proto_send_event(BLE_ADV_EVT, buf, 8 + plen);
  Bluefruit.Scanner.resume();  // Bluefruit requires an explicit resume per report
}

void ble_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_BLE, op);
  switch (op) {
    case 0x01: {  // SCAN_START: duration_s u8 (0=forever), active u8
      NEED(2);
      Bluefruit.Scanner.setRxCallback(scan_cb);
      Bluefruit.Scanner.restartOnDisconnect(true);
      Bluefruit.Scanner.setInterval(160, 80);   // 100 ms / 50 ms (units of 0.625 ms)
      Bluefruit.Scanner.useActiveScan(p[1] != 0);
      Bluefruit.Scanner.start(p[0] ? (uint16_t)(p[0] * 100) : 0);  // timeout in 10 ms units; 0 = forever
      scanning = true;
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x02:  // SCAN_STOP
      if (scanning) { Bluefruit.Scanner.stop(); scanning = false; }
      proto_reply_ok(seq, cmd);
      break;

    // GATT server (ADV_START/STOP, GATTS_*) and GATT client (GATTC_*) are not
    // available on the nRF build — see the scope note at the top of this file.
    default:
      proto_reply_err(seq, cmd, ST_UNSUPPORTED);
  }
}

#else  // !BRIDGE_BLE

UNSUPPORTED_STUB(ble_handle, MOD_BLE)

#endif
#endif  // ARDUINO_ARCH_NRF52
