// I2C master on Wire / Wire1.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include <Wire.h>

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
  NEED(1);
  TwoWire* w = bus_of(p[0]);
  if (!w) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
  uint8_t bus = p[0];

  if (op != 0x01 && !i2c_inited[bus]) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }

  switch (op) {
    case 0x01: {  // INIT: bus, sda, scl, freq u32 -> wire_buf u16
      NEED(7);
      if (i2c_inited[bus]) w->end();
      // The default 128 B Wire TX buffer silently truncates larger writes.
      // setBufferSize() requires ~2× the requested size in free heap, so try
      // progressively smaller sizes and report whichever one actually succeeded.
      uint16_t got = 0;
      for (uint16_t sz : {MAX_PAYLOAD, 512, 128}) {
        if (sz > 128 && ESP.getFreeHeap() < 2u * sz + 8192) continue;
        if (w->setBufferSize(sz) && w->begin(p[1], p[2], rd32(p + 3))) { got = sz; break; }
      }
      if (!got) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
      i2c_inited[bus] = true;
      uint8_t out[2];
      wr16(out, got);
      proto_reply(seq, cmd, out, 2);
      break;
    }

    case 0x02: {  // SCAN -> n, addr[n]
      uint8_t buf[120];  // 1 count + up to 119 addrs (0x01..0x77)
      uint8_t n = 0;
      for (uint8_t a = 1; a < 0x78; a++) {
        w->beginTransmission(a);
        if (w->endTransmission() == 0) buf[1 + n++] = a;
      }
      buf[0] = n;
      proto_reply(seq, cmd, buf, 1 + n);
      break;
    }

    case 0x03: {  // WRITE: bus, addr, data..
      NEED(2);
      w->beginTransmission(p[1]);
      // write() returns the number of bytes accepted. A short return means the
      // TX buffer overflowed and the trailing bytes were silently dropped.
      uint8_t st = ST_OK;
      if (len > 2 && w->write(p + 2, len - 2) != (size_t)(len - 2)) {
        w->endTransmission();
        st = ST_BAD_ARGS;
      } else if (w->endTransmission() != 0) {
        st = ST_IO;
      }
      if (st != ST_OK) {
        // seq 0 means the host sent a fire-and-forget write that carries no sequence
        // number, so there is no error reply channel. Log a rate-limited warning
        // instead so burst-write failures don't disappear completely.
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
      NEED(3);
      uint8_t rlen = p[2];
      uint8_t buf[255];
      size_t got = w->requestFrom(p[1], rlen);
      if (got != rlen) { proto_reply_err(seq, cmd, ST_IO); return; }
      w->readBytes(buf, rlen);  // bulk drain, not 255 virtual read() calls
      proto_reply(seq, cmd, buf, rlen);
      break;
    }

    case 0x05: {  // WRITE_READ: bus, addr, wlen, wdata[wlen], rlen -> data
      NEED(3);
      uint8_t addr = p[1], wlen = p[2];
      if (len < (uint16_t)(4 + wlen)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      uint8_t rlen = p[3 + wlen];
      w->beginTransmission(addr);
      w->write(p + 3, wlen);
      if (w->endTransmission(false) != 0) { proto_reply_err(seq, cmd, ST_IO); return; }  // repeated start
      uint8_t buf[255];
      size_t got = w->requestFrom(addr, rlen);
      if (got != rlen) { proto_reply_err(seq, cmd, ST_IO); return; }
      w->readBytes(buf, rlen);  // bulk drain, not 255 virtual read() calls
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
