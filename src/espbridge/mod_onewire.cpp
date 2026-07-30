// 1-Wire bit-timing primitives. Architecture-independent: pure Arduino GPIO
// calls, so ESP and nRF share one copy of the slot timings — which is the point,
// because a timing fix that reached only one arch would be a silent bug.
// Higher-level logic (ROM search, CRC, device drivers) runs host-side.
//
// Each bit slot masks IRQs for up to 70 µs, so these run INLINE on rx_task (the
// app core): masking interrupts on the radio core, where slow_task lives, would
// violate the BT controller's real-time deadlines.
// Wiring: 4.7 kΩ pull-up to 3V3 required. power=1 on OW_WRITE drives the line
// high push-pull after the last byte, for parasite-powered devices.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

#if BRIDGE_HAS_ONEWIRE

// The one thing that differs per arch: ESP's portENTER_CRITICAL needs a portMUX,
// vanilla FreeRTOS on nRF uses the plain Arduino pair.
#if defined(ARDUINO_ARCH_ESP32)
static portMUX_TYPE ow_mux = portMUX_INITIALIZER_UNLOCKED;
#define OW_ENTER() portENTER_CRITICAL(&ow_mux)
#define OW_EXIT()  portEXIT_CRITICAL(&ow_mux)
#else
#define OW_ENTER() noInterrupts()
#define OW_EXIT()  interrupts()
#endif

static bool ow_reset(uint8_t pin) {
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delayMicroseconds(480);
  OW_ENTER();
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(70);
  bool present = digitalRead(pin) == LOW;
  OW_EXIT();
  delayMicroseconds(410);
  return present;
}

static void ow_write_bit(uint8_t pin, uint8_t b) {
  OW_ENTER();
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delayMicroseconds(b ? 6 : 60);
  pinMode(pin, INPUT_PULLUP);
  OW_EXIT();
  delayMicroseconds(b ? 64 : 10);
}

static uint8_t ow_read_bit(uint8_t pin) {
  OW_ENTER();
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delayMicroseconds(6);
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(9);
  uint8_t r = digitalRead(pin);
  OW_EXIT();
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
  NEED(1);
  uint8_t pin = p[0];
  switch (op) {
    case 0x01: {  // RESET -> present u8
      uint8_t present = ow_reset(pin);
      proto_reply(seq, cmd, &present, 1);
      break;
    }
    case 0x02: {  // WRITE: pin|power|data..
      NEED(3);
      for (uint16_t i = 2; i < len; i++) ow_write_byte(pin, p[i]);
      if (p[1]) { pinMode(pin, OUTPUT); digitalWrite(pin, HIGH); }  // strong pull-up
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
      NEED(2);
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

#endif  // BRIDGE_HAS_ONEWIRE
