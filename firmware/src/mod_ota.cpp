// OTA: reflash the firmware over the protocol link — USB serial or BLE, no
// boot button, no cable swap. Plain Update.h byte stream into the inactive
// app slot; needs a partition table with two app slots ("Minimal SPIFFS" on
// 4 MB boards — the default "Huge APP" has none and OTA_BEGIN replies
// ST_UNSUPPORTED). Runs on net_task (flash writes block).
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

#if BRIDGE_HAS_OTA

#include <Update.h>
#include <esp_ota_ops.h>

static uint32_t ota_total = 0;

void ota_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_OTA, op);
  switch (op) {
    case 0x01: {  // BEGIN: size u32 (0xFFFFFFFF = unknown)
      if (len < 4) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (esp_ota_get_next_update_partition(nullptr) == nullptr) {
        proto_reply_err(seq, cmd, ST_UNSUPPORTED);  // no OTA slot in the table
        return;
      }
      if (Update.isRunning()) Update.abort();
      uint32_t size = rd32(p);
      ota_total = 0;
      if (!Update.begin(size == 0xFFFFFFFF ? UPDATE_SIZE_UNKNOWN : size)) {
        proto_log(2, Update.errorString());
        proto_reply_err(seq, cmd, ST_IO);
        return;
      }
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x02: {  // WRITE: data -> total_written u32
      if (!Update.isRunning()) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      if (Update.write((uint8_t*)p, len) != len) {
        proto_log(2, Update.errorString());
        Update.abort();
        proto_reply_err(seq, cmd, ST_IO);
        return;
      }
      ota_total += len;
      uint8_t buf[4];
      wr32(buf, ota_total);
      proto_reply(seq, cmd, buf, 4);
      break;
    }
    case 0x03: {  // END: commit u8 (1 = reboot into the new firmware)
      if (len < 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (!Update.end(true)) {
        proto_log(2, Update.errorString());
        proto_reply_err(seq, cmd, ST_IO);
        return;
      }
      proto_reply_ok(seq, cmd);
      if (p[0]) {
        proto_tx_flush();
        delay(50);
        ESP.restart();
      }
      break;
    }
    case 0x04:  // ABORT
      Update.abort();
      proto_reply_ok(seq, cmd);
      break;
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#else

void ota_handle(uint8_t, uint8_t seq, const uint8_t*, uint16_t) {
  proto_reply_err(seq, CMD(MOD_OTA, 0), ST_UNSUPPORTED);
}

#endif
