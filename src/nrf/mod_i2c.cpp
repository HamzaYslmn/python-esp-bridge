// I2C master (nRF52) on Wire / Wire1. Counterpart to src/esp/mod_i2c.cpp.
// The nRF52 TwoWire takes pins via setPins() (before begin) rather than
// begin(sda, scl, freq), and has no setBufferSize — the buffer is fixed, and
// I2C_INIT reports its capacity so the host chunks larger transfers to fit.
#if defined(ARDUINO_ARCH_NRF52)
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include <Wire.h>

#ifdef BUFFER_LENGTH
  #define NRF_WIRE_BUF BUFFER_LENGTH
#else
  #define NRF_WIRE_BUF 32
#endif

static bool i2c_inited[2];

static TwoWire* bus_for_index(uint8_t idx) {
  if (idx == 0) return &Wire;
#if defined(WIRE_INTERFACES_COUNT) && (WIRE_INTERFACES_COUNT > 1)
  if (idx == 1) return &Wire1;
#endif
  return nullptr;
}

void i2c_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_I2C, op);
  NEED(1);
  TwoWire* w = bus_for_index(p[0]);
  if (!w) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
  uint8_t bus = p[0];

  if (op != 0x01 && !i2c_inited[bus]) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }

  switch (op) {
    case 0x01: {  // INIT: bus, sda, scl, freq u32 -> wire_buf u16
      NEED(7);
      if (i2c_inited[bus]) w->end();
      w->setPins(p[1], p[2]);   // must precede begin() on this core
      w->begin();
      w->setClock(read_be32(p + 3));
      i2c_inited[bus] = true;
      uint8_t out[2];
      write_be16(out, NRF_WIRE_BUF);
      proto_reply(seq, cmd, out, 2);
      break;
    }

    case 0x02: {  // SCAN -> n, addr[n]
      uint8_t buf[120];
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
      uint8_t st = ST_OK;
      if (len > 2 && w->write(p + 2, len - 2) != (size_t)(len - 2)) {
        w->endTransmission();
        st = ST_BAD_ARGS;  // exceeded the Wire buffer (host should chunk to wire_buf)
      } else if (w->endTransmission() != 0) {
        st = ST_IO;        // NACK / bus error
      }
      if (st != ST_OK) {
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
      size_t got = w->requestFrom((int)p[1], (int)rlen);
      if (got != rlen) { proto_reply_err(seq, cmd, ST_IO); return; }
      w->readBytes(buf, rlen);
      proto_reply(seq, cmd, buf, rlen);
      break;
    }

    case 0x05: {  // WRITE_READ: bus, addr, wlen, wdata[wlen], rlen -> data (repeated start)
      NEED(3);
      uint8_t addr = p[1], wlen = p[2];
      if (len < (uint16_t)(4 + wlen)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      uint8_t rlen = p[3 + wlen];
      w->beginTransmission(addr);
      w->write(p + 3, wlen);
      if (w->endTransmission(false) != 0) { proto_reply_err(seq, cmd, ST_IO); return; }
      uint8_t buf[255];
      size_t got = w->requestFrom((int)addr, (int)rlen);
      if (got != rlen) { proto_reply_err(seq, cmd, ST_IO); return; }
      w->readBytes(buf, rlen);
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

#endif  // ARDUINO_ARCH_NRF52
