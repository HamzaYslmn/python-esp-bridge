#if defined(ARDUINO_ARCH_ESP32)
// I2S audio input and output using ESP_I2S in standard mode.
// The host pushes or pulls PCM data in <=2 KB chunks; the I2S DMA buffers absorb
// link-layer jitter. Link bandwidth is the hard ceiling on sample rates:
// ~92 KB/s at 921600 baud supports up to 16-bit 16 kHz mono.
// Runs on slow_task.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

#if BRIDGE_HAS_I2S

#include <ESP_I2S.h>

static I2SClass i2s;
static bool i2s_up = false;

void i2s_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_I2S, op);
  switch (op) {
    case 0x01: {  // INIT: dir|bclk|ws|dout|din|rate u32|bits|stereo
      NEED(11);
      if (i2s_up) { proto_reply_err(seq, cmd, ST_BUSY); return; }
      i2s_data_bit_width_t bits =
          p[9] == 8 ? I2S_DATA_BIT_WIDTH_8BIT
        : p[9] == 24 ? I2S_DATA_BIT_WIDTH_24BIT
        : p[9] == 32 ? I2S_DATA_BIT_WIDTH_32BIT : I2S_DATA_BIT_WIDTH_16BIT;
      i2s.setPins((int8_t)p[1], (int8_t)p[2], (int8_t)p[3], (int8_t)p[4]);
      if (!i2s.begin(I2S_MODE_STD, read_be32(p + 5), bits,
                     p[10] ? I2S_SLOT_MODE_STEREO : I2S_SLOT_MODE_MONO)) {
        proto_reply_err(seq, cmd, ST_IO);
        return;
      }
      i2s_up = true;
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x02: {  // WRITE: pcm -> written u16
      if (!i2s_up) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      size_t w = i2s.write(p, len);
      uint8_t buf[2];
      write_be16(buf, w);
      proto_reply(seq, cmd, buf, 2);
      break;
    }
    case 0x03: {  // READ: n u16 -> pcm
      NEED(2);
      if (!i2s_up) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      uint16_t n = read_be16(p);
      if (n > MAX_PAYLOAD - 8) n = MAX_PAYLOAD - 8;
      uint8_t* buf = (uint8_t*)malloc(n ? n : 1);
      if (!buf) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
      size_t got = i2s.readBytes((char*)buf, n);
      proto_reply(seq, cmd, buf, got);
      free(buf);
      break;
    }
    case 0x04:  // DEINIT
      if (i2s_up) {
        i2s.end();
        i2s_up = false;
      }
      proto_reply_ok(seq, cmd);
      break;
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#else

UNSUPPORTED_STUB(i2s_handle, MOD_I2S)

#endif
#endif  // ARDUINO_ARCH_ESP32
