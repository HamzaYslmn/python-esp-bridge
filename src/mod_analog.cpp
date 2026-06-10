// ADC (oneshot reads), DAC (write + cosine generator), TOUCH.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/radio.h"
#include <esp32-hal-adc.h>

#if BRIDGE_HAS_DAC
#include <esp32-hal-dac.h>
#if __has_include("driver/dac_cosine.h")
#include "driver/dac_cosine.h"
#define BRIDGE_DAC_COSINE 1
#endif
#endif

#if BRIDGE_HAS_TOUCH
#include <esp32-hal-touch.h>
#endif

// pin_is_adc2() — the ADC2/Wi-Fi conflict check — is shared via modules.h.

#if BRIDGE_HAS_DAC
static bool dac_pin(uint8_t pin) {
#if defined(CONFIG_IDF_TARGET_ESP32)
  return pin == 25 || pin == 26;
#elif defined(CONFIG_IDF_TARGET_ESP32S2)
  return pin == 17 || pin == 18;
#else
  return false;
#endif
}

#if BRIDGE_DAC_COSINE
static dac_cosine_handle_t cos_handle[2];  // per DAC channel
static int dac_chan(uint8_t pin) {
#if defined(CONFIG_IDF_TARGET_ESP32)
  return pin == 25 ? 0 : 1;
#else
  return pin == 17 ? 0 : 1;
#endif
}

static void cos_stop(uint8_t pin) {
  int ch = dac_chan(pin);
  if (cos_handle[ch]) {
    dac_cosine_stop(cos_handle[ch]);
    dac_cosine_del_channel(cos_handle[ch]);
    cos_handle[ch] = nullptr;
  }
}
#endif
#endif  // BRIDGE_HAS_DAC

static void adc_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_ADC, op);
  NEED(1);
  uint8_t pin = p[0];
  switch (op) {
    case 0x01: {  // CONFIG: pin, atten
      if (len < 2 || p[1] > 3) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      analogSetPinAttenuation(pin, (adc_attenuation_t)p[1]);
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x02:    // READ -> raw u16
    case 0x03: {  // READ_MV -> millivolts u16
      if (pin_is_adc2(pin) && radio_active()) { proto_reply_err(seq, cmd, ST_BUSY); return; }
      uint8_t buf[2];
      wr16(buf, op == 0x02 ? (uint16_t)analogRead(pin)
                           : (uint16_t)analogReadMilliVolts(pin));
      proto_reply(seq, cmd, buf, 2);
      break;
    }
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

static void dac_handle_(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_DAC, op);
#if !BRIDGE_HAS_DAC
  (void)p; (void)len;
  proto_reply_err(seq, cmd, ST_UNSUPPORTED);
#else
  NEED(1);
  uint8_t pin = p[0];
  if (!dac_pin(pin)) { proto_reply_err(seq, cmd, ST_BAD_PIN); return; }
  switch (op) {
    case 0x01:  // WRITE: pin, value
      NEED(2);
#if BRIDGE_DAC_COSINE
      cos_stop(pin);  // the cosine generator holds exclusive ownership of the DAC channel while active, so stop it before doing a plain write
#endif
      dacWrite(pin, p[1]);
      proto_reply_ok(seq, cmd);
      break;

    case 0x02: {  // COSINE: pin, freq u32, scale, offset i8, phase
#if BRIDGE_DAC_COSINE
      NEED(8);
      cos_stop(pin);
      dacDisable(pin);
      dac_cosine_config_t cfg = {};
      cfg.chan_id = (dac_channel_t)dac_chan(pin);
      cfg.freq_hz = rd32(p + 1);
      cfg.clk_src = DAC_COSINE_CLK_SRC_DEFAULT;
      cfg.atten = (dac_cosine_atten_t)p[5];
      cfg.phase = p[7] ? DAC_COSINE_PHASE_180 : DAC_COSINE_PHASE_0;
      cfg.offset = (int8_t)p[6];
      cfg.flags.force_set_freq = true;
      int ch = dac_chan(pin);
      if (dac_cosine_new_channel(&cfg, &cos_handle[ch]) != ESP_OK ||
          dac_cosine_start(cos_handle[ch]) != ESP_OK) {
        if (cos_handle[ch]) { dac_cosine_del_channel(cos_handle[ch]); cos_handle[ch] = nullptr; }
        proto_reply_err(seq, cmd, ST_IO);
        return;
      }
      proto_reply_ok(seq, cmd);
#else
      proto_reply_err(seq, cmd, ST_UNSUPPORTED);
#endif
      break;
    }

    case 0x03:  // COS_STOP
#if BRIDGE_DAC_COSINE
      cos_stop(pin);
#endif
      proto_reply_ok(seq, cmd);
      break;

    case 0x04:  // DISABLE
#if BRIDGE_DAC_COSINE
      cos_stop(pin);
#endif
      dacDisable(pin);
      proto_reply_ok(seq, cmd);
      break;

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
#endif
}

static void touch_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_TOUCH, op);
#if !BRIDGE_HAS_TOUCH
  (void)p; (void)len;
  proto_reply_err(seq, cmd, ST_UNSUPPORTED);
#else
  if (op != 0x01) { proto_reply_err(seq, cmd, ST_UNKNOWN_CMD); return; }
  NEED(1);
  uint8_t buf[4];
  wr32(buf, (uint32_t)touchRead(p[0]));
  proto_reply(seq, cmd, buf, 4);
#endif
}

void analog_handle(uint8_t mod, uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  switch (mod) {
    case MOD_ADC:   adc_handle(op, seq, p, len); break;
    case MOD_DAC:   dac_handle_(op, seq, p, len); break;
    case MOD_TOUCH: touch_handle(op, seq, p, len); break;
  }
}
