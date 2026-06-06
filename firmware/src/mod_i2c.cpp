// I2C master on Wire / Wire1.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include <Wire.h>
#include <esp_heap_caps.h>

static bool i2c_inited[2];

static TwoWire* bus_of(uint8_t idx) {
  if (idx == 0) return &Wire;
#if SOC_I2C_NUM > 1
  if (idx == 1) return &Wire1;  // C3/C6/H2 have a single I2C controller
#endif
  return nullptr;
}

void i2c_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_I2C, op);
  if (len < 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
  TwoWire* w = bus_of(p[0]);
  if (!w) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
  uint8_t bus = p[0];

  if (op != 0x01 && !i2c_inited[bus]) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }

  switch (op) {
    case 0x01: {  // INIT: bus, sda, scl, freq u32 -> wire_buf u16
      if (len < 7) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (i2c_inited[bus]) w->end();
      // Wire's default TX buffer is 128 bytes and write() silently drops
      // anything beyond it; size it to fit any I2C_WRITE payload. begin()
      // allocates 2x this from the heap — which a classic ESP32 with the
      // BLE link connected runs within a few KB of — so fall back to
      // smaller buffers instead of failing, and report the size that stuck
      // (hosts chunk their writes to it).
      //
      // The fallback must be heap-aware, not try-and-see (all measured on
      // classic ESP32 + BLE link, core 3.3.6):
      //  - the IDF i2c driver install after the buffer allocation eats
      //    ~2 KB and WEDGES rx_task silently below ~7 KB free (no panic,
      //    no error return — the board just stops answering);
      //  - the radio stacks need ~6.5 KB free *for the rest of the
      //    session*, or Bluedroid starts dropping replies under load
      //    (6.7 KB rest = solid; 5.9 KB rest = host-visible timeouts).
      // So a buffer above the 128-byte floor must leave 2*size + ~9 KB,
      // plus a contiguous chunk to spare.
      static const uint16_t sizes[] = {MAX_PAYLOAD, 1024, 512, 256, 128};
      uint16_t got = 0;
      for (uint8_t i = 0; i < 5 && !got; i++) {
        if (sizes[i] > 128) {
          if (ESP.getFreeHeap() < 2u * sizes[i] + 9216) continue;
          if (heap_caps_get_largest_free_block(MALLOC_CAP_8BIT)
              < (size_t)sizes[i] + 2048) continue;
        }
        if (w->setBufferSize(sizes[i]) && w->begin(p[1], p[2], rd32(p + 3)))
          got = sizes[i];
      }
      if (!got) {
        proto_log_heap("i2c: init failed");  // ST_IO alone is opaque
        proto_reply_err(seq, cmd, ST_IO);
        return;
      }
      i2c_inited[bus] = true;
      uint8_t out[2] = {(uint8_t)(got >> 8), (uint8_t)got};
      proto_reply(seq, cmd, out, 2);
      break;
    }

    case 0x02: {  // SCAN -> n, addr[n]
      uint8_t found[128];
      uint8_t n = 0;
      for (uint8_t a = 1; a < 0x78; a++) {
        w->beginTransmission(a);
        if (w->endTransmission() == 0) found[n++] = a;
      }
      uint8_t buf[129];
      buf[0] = n;
      memcpy(buf + 1, found, n);
      proto_reply(seq, cmd, buf, 1 + n);
      break;
    }

    case 0x03: {  // WRITE: bus, addr, data..
      if (len < 2) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      w->beginTransmission(p[1]);
      // write() returns bytes buffered — short means TX buffer overflow,
      // which would silently corrupt the transfer (drops the tail bytes)
      uint8_t st = ST_OK;
      if (len > 2 && w->write(p + 2, len - 2) != (size_t)(len - 2)) {
        w->endTransmission();
        st = ST_BAD_ARGS;
      } else if (w->endTransmission() != 0) {
        st = ST_IO;
      }
      if (st != ST_OK) {
        // Fire-and-forget (seq 0) gets no error reply, so a failed write
        // in a pipelined burst (OLED frame push) would vanish without a
        // trace — visible only as display corruption. Warn, rate-limited.
        if (seq == 0) {
          static uint32_t last_warn = 0;
          uint32_t now = millis();
          if (now - last_warn > 1000) {
            last_warn = now;
            proto_log(2, st == ST_IO ? "i2c: unacked write failed (NACK/bus)"
                                     : "i2c: unacked write exceeds wire buffer");
          }
        }
        proto_reply_err(seq, cmd, st);
        return;
      }
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x04: {  // READ: bus, addr, len -> data
      if (len < 3) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      uint8_t rlen = p[2];
      uint8_t buf[255];
      size_t got = w->requestFrom(p[1], rlen);
      if (got != rlen) { proto_reply_err(seq, cmd, ST_IO); return; }
      for (uint8_t i = 0; i < rlen; i++) buf[i] = w->read();
      proto_reply(seq, cmd, buf, rlen);
      break;
    }

    case 0x05: {  // WRITE_READ: bus, addr, wlen, wdata[wlen], rlen -> data
      if (len < 3) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      uint8_t addr = p[1], wlen = p[2];
      if (len < (uint16_t)(4 + wlen)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      uint8_t rlen = p[3 + wlen];
      w->beginTransmission(addr);
      w->write(p + 3, wlen);
      if (w->endTransmission(false) != 0) { proto_reply_err(seq, cmd, ST_IO); return; }  // repeated start
      uint8_t buf[255];
      size_t got = w->requestFrom(addr, rlen);
      if (got != rlen) { proto_reply_err(seq, cmd, ST_IO); return; }
      for (uint8_t i = 0; i < rlen; i++) buf[i] = w->read();
      proto_reply(seq, cmd, buf, rlen);
      break;
    }

    case 0x06: {  // DEINIT
      w->end();
      i2c_inited[bus] = false;
      proto_reply_ok(seq, cmd);
      break;
    }

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}
