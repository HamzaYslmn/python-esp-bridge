// PWM via LEDC (arduino-esp32 3.x pin-based API).
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

void pwm_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_PWM, op);
  switch (op) {
    case 0x01: {  // ATTACH: pin, freq u32, res_bits
      NEED(6);
      uint8_t pin = p[0], res = p[5];
      uint32_t freq = rd32(p + 1);
      if (res < 1 || res > 16 || freq == 0) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (!ledcAttach(pin, freq, res)) { proto_reply_err(seq, cmd, ST_IO); return; }
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x02: {  // WRITE: pin, duty u32
      NEED(5);
      if (!ledcWrite(p[0], rd32(p + 1))) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x03: {  // DETACH: pin
      NEED(1);
      ledcDetach(p[0]);
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x04: {  // TONE: pin, freq u32 (0 = off)
      NEED(5);
      ledcWriteTone(p[0], rd32(p + 1));
      proto_reply_ok(seq, cmd);
      break;
    }
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}
