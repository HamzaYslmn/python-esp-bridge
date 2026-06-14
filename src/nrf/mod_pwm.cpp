// PWM (nRF52): maps the protocol's ATTACH(freq,res)/WRITE(duty) model onto the
// nRF HardwarePWM peripheral. Each attached pin claims one of the (3-4) PWM
// instances, so up to that many independent PWM pins are supported at once.
// Counterpart to src/esp/mod_pwm.cpp (which uses the ESP LEDC API).
#if defined(ARDUINO_ARCH_NRF52)
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

// The nRF PWM countertop is 15-bit, so the duty resolution caps at 14 bits
// (1<<15 = 32768 exceeds the 0x7FFF countertop limit).
static HardwarePWM* const PWM_INST[] = {
  &HwPWM0, &HwPWM1, &HwPWM2,
#ifdef NRF_PWM3
  &HwPWM3,
#endif
};
static const uint8_t PWM_N = sizeof(PWM_INST) / sizeof(PWM_INST[0]);

static HardwarePWM* inst_for_pin(uint8_t pin) {
  for (uint8_t i = 0; i < PWM_N; i++)
    if (PWM_INST[i]->pin2channel(pin) >= 0) return PWM_INST[i];
  return nullptr;
}

static HardwarePWM* free_inst() {
  for (uint8_t i = 0; i < PWM_N; i++)
    if (PWM_INST[i]->usedChannelCount() == 0) return PWM_INST[i];
  return nullptr;
}

// Pick the prescaler (0..7 → divide 16 MHz by 1..128) whose resulting frequency
// (16 MHz / div / countertop) is closest to the requested one.
static uint8_t pick_div(uint32_t freq, uint16_t top) {
  uint8_t best = 0;
  uint32_t best_err = 0xFFFFFFFFUL;
  for (uint8_t d = 0; d <= 7; d++) {
    uint32_t f = (16000000UL >> d) / top;
    uint32_t err = f > freq ? f - freq : freq - f;
    if (err < best_err) { best_err = err; best = d; }
  }
  return best;
}

// Configure a free instance for (freq, top) and bind `pin` to it. Returns the
// instance, or nullptr if all instances are busy / the pin can't be added.
static HardwarePWM* setup_pin(uint8_t pin, uint32_t freq, uint16_t top) {
  HardwarePWM* h = inst_for_pin(pin);
  if (h) h->removePin(pin);            // reconfiguring: drop the old binding first
  h = free_inst();
  if (!h) return nullptr;
  h->setClockDiv(pick_div(freq, top));
  h->setMaxValue(top);
  if (!h->addPin(pin)) return nullptr;
  return h;
}

void pwm_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_PWM, op);
  switch (op) {
    case 0x01: {  // ATTACH: pin, freq u32, res_bits
      NEED(6);
      uint8_t pin = p[0], res = p[5];
      uint32_t freq = read_be32(p + 1);
      if (res < 1 || res > 14 || freq == 0) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      uint16_t top = (uint16_t)(1u << res);
      if (!setup_pin(pin, freq, top)) { proto_reply_err(seq, cmd, ST_BUSY); return; }
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x02: {  // WRITE: pin, duty u32
      NEED(5);
      HardwarePWM* h = inst_for_pin(p[0]);
      if (!h) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      h->writePin(p[0], (uint16_t)read_be32(p + 1));
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x03: {  // DETACH: pin
      NEED(1);
      HardwarePWM* h = inst_for_pin(p[0]);
      if (h) h->removePin(p[0]);
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x04: {  // TONE: pin, freq u32 (0 = off); square wave at 50% duty
      NEED(5);
      uint8_t pin = p[0];
      uint32_t freq = read_be32(p + 1);
      if (freq == 0) {
        HardwarePWM* h = inst_for_pin(pin);
        if (h) h->removePin(pin);
        proto_reply_ok(seq, cmd);
        return;
      }
      const uint16_t top = 1024;
      HardwarePWM* h = setup_pin(pin, freq, top);
      if (!h) { proto_reply_err(seq, cmd, ST_BUSY); return; }
      h->writePin(pin, top / 2);
      proto_reply_ok(seq, cmd);
      break;
    }
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#endif  // ARDUINO_ARCH_NRF52
