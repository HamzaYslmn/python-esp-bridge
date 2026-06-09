// 1-Wire bit-timing primitives. Higher-level logic (ROM search, CRC, device
// drivers) runs host-side to keep firmware simple.
// Each bit slot masks IRQs for up to 70 µs, so these run on net_task to avoid
// starving time-critical tasks.
// Wiring: 4.7 kΩ pull-up to 3V3 required.
// power=1 on OW_WRITE drives the line high push-pull after the last byte,
// supplying parasite power to bus-powered devices.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

#if BRIDGE_HAS_ONEWIRE

static portMUX_TYPE ow_mux = portMUX_INITIALIZER_UNLOCKED;

static bool ow_reset(uint8_t pin) {
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delayMicroseconds(480);
  portENTER_CRITICAL(&ow_mux);
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(70);
  bool present = digitalRead(pin) == LOW;
  portEXIT_CRITICAL(&ow_mux);
  delayMicroseconds(410);
  return present;
}

static void ow_write_bit(uint8_t pin, uint8_t b) {
  portENTER_CRITICAL(&ow_mux);
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delayMicroseconds(b ? 6 : 60);
  pinMode(pin, INPUT_PULLUP);
  portEXIT_CRITICAL(&ow_mux);
  delayMicroseconds(b ? 64 : 10);
}

static uint8_t ow_read_bit(uint8_t pin) {
  portENTER_CRITICAL(&ow_mux);
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delayMicroseconds(6);
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(9);
  uint8_t r = digitalRead(pin);
  portEXIT_CRITICAL(&ow_mux);
  delayMicroseconds(55);
  return r;
}

static void ow_write_byte(uint8_t pin, uint8_t v) {
  for (uint8_t i = 0; i < 8; i++) ow_write_bit(pin, (v >> i) & 1);  // LSB first
}

static uint8_t ow_read_byte(uint8_t pin) {
  uint8_t v = 0;
  for (uint8_t i = 0; i < 8; i++) v |= ow_read_bit(pin) << i;
  return v;
}

void onewire_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_ONEWIRE, op);
  if (len < 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
  uint8_t pin = p[0];
  switch (op) {
    case 0x01: {  // RESET -> present u8
      uint8_t present = ow_reset(pin);
      proto_reply(seq, cmd, &present, 1);
      break;
    }
    case 0x02: {  // WRITE: pin|power|data..
      if (len < 3) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      for (uint16_t i = 2; i < len; i++) ow_write_byte(pin, p[i]);
      if (p[1]) { pinMode(pin, OUTPUT); digitalWrite(pin, HIGH); }  // hold line high (strong pull-up) for parasite-powered devices
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x03: {  // READ: pin|n -> data[n]
      if (len < 2 || p[1] == 0) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      uint8_t buf[255];
      for (uint8_t i = 0; i < p[1]; i++) buf[i] = ow_read_byte(pin);
      proto_reply(seq, cmd, buf, p[1]);
      break;
    }
    case 0x04: {  // TRIPLET: pin|dir -> id_bit|cmp_bit|taken (ROM search step)
      if (len < 2) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      uint8_t out[3];
      out[0] = ow_read_bit(pin);
      out[1] = ow_read_bit(pin);
      out[2] = (out[0] != out[1]) ? out[0] : (out[0] ? 1 : p[1]);
      if (!(out[0] && out[1])) ow_write_bit(pin, out[2]);
      proto_reply(seq, cmd, out, 3);
      break;
    }
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#else

UNSUPPORTED_STUB(onewire_handle, MOD_ONEWIRE)

#endif
