// SPI master (nRF52), full-duplex with optional CS handling. Counterpart to
// src/esp/mod_spi.cpp. Single host (the core's `SPI` / SPIM); the nRF SPI API
// takes pins via setPins(miso, sck, mosi) and does in-place full-duplex with
// transfer(buf, count).
#if defined(ARDUINO_ARCH_NRF52)
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include <SPI.h>

struct SpiState { bool inited; uint32_t freq; uint8_t mode; uint8_t msb; };
static SpiState st0;

void spi_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_SPI, op);
  NEED(1);
  if (p[0] != 0) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }  // single SPI host on this build

  switch (op) {
    case 0x01: {  // INIT: host, sck i8, miso i8, mosi i8, freq u32, mode, msb_first
      NEED(10);
      if (st0.inited) SPI.end();
      int8_t sck = (int8_t)p[1], miso = (int8_t)p[2], mosi = (int8_t)p[3];
      if (sck >= 0 && miso >= 0 && mosi >= 0)
        SPI.setPins((uint8_t)miso, (uint8_t)sck, (uint8_t)mosi);  // else: variant default pins
      SPI.begin();
      st0 = { true, read_be32(p + 4), (uint8_t)(p[8] & 3), p[9] };
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x02: {  // TRANSFER: host, cs i8 (-1 = no CS), data.. -> rx data (full-duplex, in place)
      if (!st0.inited) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      NEED(2);
      int8_t cs = (int8_t)p[1];
      uint8_t* buf = (uint8_t*)(p + 2);
      uint16_t n = len - 2;
      SPI.beginTransaction(SPISettings(st0.freq, st0.msb ? MSBFIRST : LSBFIRST, st0.mode));
      if (cs >= 0) { pinMode(cs, OUTPUT); digitalWrite(cs, LOW); }
      SPI.transfer(buf, n);  // in-place: tx then overwritten with rx
      if (cs >= 0) digitalWrite(cs, HIGH);
      SPI.endTransaction();
      proto_reply(seq, cmd, buf, n);
      break;
    }

    case 0x03:  // DEINIT
      if (st0.inited) { SPI.end(); st0.inited = false; }
      proto_reply_ok(seq, cmd);
      break;

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#endif  // ARDUINO_ARCH_NRF52
