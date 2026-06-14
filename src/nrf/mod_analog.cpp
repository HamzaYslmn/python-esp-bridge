// ADC (nRF52): oneshot reads. DAC and TOUCH are absent on this part and reply
// ST_UNSUPPORTED. Counterpart to src/esp/mod_analog.cpp.
//
// The nRF52 Arduino core has no analogReadMilliVolts/attenuation API. SAADC is
// configured here for 12-bit reads; with the default reference (internal 0.6 V,
// gain 1/6 → ~0..3.6 V full scale) the nominal conversion is mV ≈ raw*3600/4095.
// This is uncalibrated (no per-chip eFuse correction like the ESP path).
#if defined(ARDUINO_ARCH_NRF52)
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

static void adc_setup_once() {
  static bool done = false;
  if (!done) { analogReadResolution(12); done = true; }
}

static void handle_adc(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_ADC, op);
  NEED(1);
  uint8_t pin = p[0];
  switch (op) {
    case 0x01:  // CONFIG: pin, atten — no per-pin attenuation on nRF52; accept and no-op
      if (len < 2 || p[1] > 3) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      adc_setup_once();
      proto_reply_ok(seq, cmd);
      break;
    case 0x02:    // READ -> raw u16 (12-bit)
    case 0x03: {  // READ_MV -> millivolts u16 (nominal)
      adc_setup_once();
      uint16_t raw = (uint16_t)analogRead(pin);
      uint8_t buf[2];
      write_be16(buf, op == 0x02 ? raw : (uint16_t)((uint32_t)raw * 3600u / 4095u));
      proto_reply(seq, cmd, buf, 2);
      break;
    }
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

void analog_handle(uint8_t mod, uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  if (mod == MOD_ADC) { handle_adc(op, seq, p, len); return; }
  proto_reply_err(seq, CMD(mod, op), ST_UNSUPPORTED);  // DAC / TOUCH absent on nRF52
}

#endif  // ARDUINO_ARCH_NRF52
