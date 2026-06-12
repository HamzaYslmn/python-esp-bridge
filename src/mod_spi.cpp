// SPI master, full-duplex transfers with optional CS handling.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include <SPI.h>

static SPIClass spi0(BRIDGE_SPI_HOST0);
#if BRIDGE_SPI_HOST1 != BRIDGE_SPI_HOST0
static SPIClass spi1(BRIDGE_SPI_HOST1);
#endif

struct SpiState { bool inited; uint32_t freq; uint8_t mode; uint8_t msb; };
static SpiState st[2];

static SPIClass* host_for_index(uint8_t idx) {
  if (idx == 0) return &spi0;
#if BRIDGE_SPI_HOST1 != BRIDGE_SPI_HOST0
  if (idx == 1) return &spi1;
#endif
  return nullptr;
}

void spi_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_SPI, op);
  NEED(1);
  uint8_t idx = p[0];
  SPIClass* spi = host_for_index(idx);
  if (!spi) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }

  switch (op) {
    case 0x01: {  // INIT: host, sck i8, miso i8, mosi i8, freq u32, mode, msb_first
      NEED(10);
      if (st[idx].inited) spi->end();
      spi->begin((int8_t)p[1], (int8_t)p[2], (int8_t)p[3], -1);
      st[idx] = { true, read_be32(p + 4), (uint8_t)(p[8] & 3), p[9] };
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x02: {  // TRANSFER: host, cs i8 (-1 = no CS), data.. -> rx data (full-duplex)
      if (!st[idx].inited) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      NEED(2);
      int8_t cs = (int8_t)p[1];
      // Full-duplex in place over the payload (rx_task's COBS accumulator, ours
      // until return) — same idiom as SPI.transfer(buf, n); saves a 2 KB buffer.
      uint8_t* buf = (uint8_t*)(p + 2);
      uint16_t n = len - 2;
      spi->beginTransaction(SPISettings(st[idx].freq,
                                        st[idx].msb ? MSBFIRST : LSBFIRST,
                                        st[idx].mode));
      if (cs >= 0) { pinMode(cs, OUTPUT); digitalWrite(cs, LOW); }
      spi->transferBytes(buf, buf, n);
      if (cs >= 0) digitalWrite(cs, HIGH);
      spi->endTransaction();
      proto_reply(seq, cmd, buf, n);
      break;
    }

    case 0x03: {  // DEINIT
      if (st[idx].inited) { spi->end(); st[idx].inited = false; }
      proto_reply_ok(seq, cmd);
      break;
    }

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}
