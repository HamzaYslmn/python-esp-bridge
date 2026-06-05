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

static SPIClass* host_of(uint8_t idx) {
  if (idx == 0) return &spi0;
#if BRIDGE_SPI_HOST1 != BRIDGE_SPI_HOST0
  if (idx == 1) return &spi1;
#endif
  return nullptr;
}

static uint8_t spi_rx[MAX_PAYLOAD];

void spi_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_SPI, op);
  if (len < 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
  uint8_t idx = p[0];
  SPIClass* spi = host_of(idx);
  if (!spi) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }

  switch (op) {
    case 0x01: {  // INIT: host, sck i8, miso i8, mosi i8, freq u32, mode, msb_first
      if (len < 10) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (st[idx].inited) spi->end();
      spi->begin((int8_t)p[1], (int8_t)p[2], (int8_t)p[3], -1);
      st[idx] = { true, rd32(p + 4), (uint8_t)(p[8] & 3), p[9] };
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x02: {  // TRANSFER: host, cs i8, data.. -> rx data
      if (!st[idx].inited) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      if (len < 2) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      int8_t cs = (int8_t)p[1];
      const uint8_t* tx = p + 2;
      uint16_t n = len - 2;
      spi->beginTransaction(SPISettings(st[idx].freq,
                                        st[idx].msb ? MSBFIRST : LSBFIRST,
                                        st[idx].mode));
      if (cs >= 0) { pinMode(cs, OUTPUT); digitalWrite(cs, LOW); }
      spi->transferBytes(tx, spi_rx, n);
      if (cs >= 0) digitalWrite(cs, HIGH);
      spi->endTransaction();
      proto_reply(seq, cmd, spi_rx, n);
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
