// I2C master on Wire / Wire1.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include <Wire.h>

static bool i2c_inited[2];

static TwoWire* bus_of(uint8_t idx) {
  if (idx == 0) return &Wire;
  if (idx == 1) return &Wire1;
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
    case 0x01: {  // INIT: bus, sda, scl, freq u32
      if (len < 7) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (i2c_inited[bus]) w->end();
      // Wire's default TX buffer is 128 bytes and write() silently drops
      // anything beyond it; size it to fit any I2C_WRITE payload.
      if (w->setBufferSize(MAX_PAYLOAD) == 0) { proto_reply_err(seq, cmd, ST_IO); return; }
      if (!w->begin(p[1], p[2], rd32(p + 3))) { proto_reply_err(seq, cmd, ST_IO); return; }
      i2c_inited[bus] = true;
      proto_reply_ok(seq, cmd);
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
      if (len > 2 && w->write(p + 2, len - 2) != (size_t)(len - 2)) {
        w->endTransmission();
        proto_reply_err(seq, cmd, ST_BAD_ARGS);
        return;
      }
      if (w->endTransmission() != 0) { proto_reply_err(seq, cmd, ST_IO); return; }
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
