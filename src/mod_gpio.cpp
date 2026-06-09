// GPIO: modes, read/write, batch ops, edge interrupts -> events.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include <esp_timer.h>

struct EdgeRaw { uint8_t pin; uint8_t level; uint32_t ms; };
static QueueHandle_t edgeq;

static uint16_t debounce_ms[SOC_GPIO_PIN_COUNT];   // per watched pin
static uint32_t last_edge_ms[SOC_GPIO_PIN_COUNT];
static uint8_t pin_mode[SOC_GPIO_PIN_COUNT];       // last mode set via SET_MODE (0xFF = unknown)

static bool valid_pin(uint8_t pin) {
  return pin < SOC_GPIO_PIN_COUNT && GPIO_IS_VALID_GPIO((gpio_num_t)pin);
}

static bool strap_pin(uint8_t pin) {
#if defined(CONFIG_IDF_TARGET_ESP32)
  return pin == 0 || pin == 2 || pin == 12 || pin == 15;
#elif defined(CONFIG_IDF_TARGET_ESP32S3)
  return pin == 0 || pin == 3 || pin == 45 || pin == 46;
#else
  return pin == 0;
#endif
}

static void IRAM_ATTR gpio_isr(void* arg) {
  uint8_t pin = (uint8_t)(uintptr_t)arg;
  EdgeRaw e = { pin, (uint8_t)digitalRead(pin), (uint32_t)(esp_timer_get_time() / 1000ULL) };
  BaseType_t hpw = pdFALSE;
  xQueueSendFromISR(edgeq, &e, &hpw);
  if (hpw) portYIELD_FROM_ISR();
}

void gpio_init() {
  edgeq = xQueueCreate(64, sizeof(EdgeRaw));
  memset(pin_mode, 0xFF, sizeof(pin_mode));  // 0xFF = mode not set via the bridge
}

void gpio_poll() {
  EdgeRaw e;
  while (xQueueReceive(edgeq, &e, 0) == pdTRUE) {
    if (e.pin >= SOC_GPIO_PIN_COUNT) continue;
    uint16_t db = debounce_ms[e.pin];
    if (db && (uint32_t)(e.ms - last_edge_ms[e.pin]) < db) continue;
    last_edge_ms[e.pin] = e.ms;
    uint8_t buf[6];
    buf[0] = e.pin;
    buf[1] = e.level;
    wr32(buf + 2, e.ms);
    proto_send_event(GPIO_EDGE_EVT, buf, 6);
  }
}

void gpio_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_GPIO, op);
  switch (op) {
    case 0x01: {  // SET_MODE: pin, mode
      NEED(2);
      uint8_t pin = p[0], mode = p[1];
      if (!valid_pin(pin)) { proto_reply_err(seq, cmd, ST_BAD_PIN); return; }
      switch (mode) {
        case 0: pinMode(pin, INPUT); break;
        case 1: pinMode(pin, OUTPUT); break;
        case 2: pinMode(pin, INPUT_PULLUP); break;
        case 3: pinMode(pin, INPUT_PULLDOWN); break;
        case 4: pinMode(pin, OUTPUT_OPEN_DRAIN); break;
        default: proto_reply_err(seq, cmd, ST_BAD_ARGS); return;
      }
      pin_mode[pin] = mode;  // remembered so GPIO_STATUS can report it
      proto_reply_ok(seq, cmd);
      if (strap_pin(pin)) proto_log(1, "strap pin: state at boot affects boot mode");
      break;
    }

    case 0x02: {  // WRITE: pin, value -> level read back
      NEED(2);
      if (!valid_pin(p[0])) { proto_reply_err(seq, cmd, ST_BAD_PIN); return; }
      digitalWrite(p[0], p[1] ? HIGH : LOW);
      // Read the pin straight back so the host gets the actual resulting level,
      // not just "command executed". For a push-pull output this echoes the
      // driven value; for open-drain / input it reports the real pad state.
      uint8_t v = (uint8_t)digitalRead(p[0]);
      proto_reply(seq, cmd, &v, 1);
      break;
    }

    case 0x03: {  // READ: pin -> value
      NEED(1);
      if (!valid_pin(p[0])) { proto_reply_err(seq, cmd, ST_BAD_PIN); return; }
      uint8_t v = (uint8_t)digitalRead(p[0]);
      proto_reply(seq, cmd, &v, 1);
      break;
    }

    case 0x04: {  // WRITE_MASK: mask u64, values u64
      NEED(16);
      uint64_t mask = ((uint64_t)rd32(p) << 32) | rd32(p + 4);
      uint64_t vals = ((uint64_t)rd32(p + 8) << 32) | rd32(p + 12);
      for (uint8_t pin = 0; pin < SOC_GPIO_PIN_COUNT && pin < 64; pin++)
        if ((mask >> pin) & 1 && valid_pin(pin))
          digitalWrite(pin, ((vals >> pin) & 1) ? HIGH : LOW);
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x05: {  // READ_ALL -> levels u64
      uint64_t levels = 0;
      for (uint8_t pin = 0; pin < SOC_GPIO_PIN_COUNT && pin < 64; pin++)
        if (valid_pin(pin) && digitalRead(pin)) levels |= (1ULL << pin);
      uint8_t buf[8];
      wr32(buf, (uint32_t)(levels >> 32));
      wr32(buf + 4, (uint32_t)levels);
      proto_reply(seq, cmd, buf, 8);
      break;
    }

    case 0x06: {  // WATCH: pin, edge, debounce_ms u16
      NEED(4);
      uint8_t pin = p[0], edge = p[1];
      if (!valid_pin(pin)) { proto_reply_err(seq, cmd, ST_BAD_PIN); return; }
      int mode = edge == 1 ? RISING : edge == 2 ? FALLING : edge == 3 ? CHANGE : -1;
      if (mode < 0) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      debounce_ms[pin] = rd16(p + 2);
      last_edge_ms[pin] = 0;
      attachInterruptArg(digitalPinToInterrupt(pin), gpio_isr, (void*)(uintptr_t)pin, mode);
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x07: {  // UNWATCH: pin
      NEED(1);
      if (!valid_pin(p[0])) { proto_reply_err(seq, cmd, ST_BAD_PIN); return; }
      detachInterrupt(digitalPinToInterrupt(p[0]));
      debounce_ms[p[0]] = 0;
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x08: {  // STATUS: pin -> level u8|mode u8|pwm_freq u32|pwm_duty u32
      NEED(1);
      uint8_t pin = p[0];
      if (!valid_pin(pin)) { proto_reply_err(seq, cmd, ST_BAD_PIN); return; }
      // Returns a full snapshot from the chip: the live pad level, the mode last
      // set via SET_MODE, and the LEDC (PWM) channel state (freq 0 = no PWM on
      // this pin).
      uint8_t buf[10];
      buf[0] = (uint8_t)digitalRead(pin);
      buf[1] = pin_mode[pin];
      wr32(buf + 2, ledcReadFreq(pin));  // 0 if no PWM channel on this pin
      wr32(buf + 6, ledcRead(pin));      // current raw duty (0 if none)
      proto_reply(seq, cmd, buf, 10);
      break;
    }

    case 0x09: {  // DUMP: every active pin in one frame ->
      //   count u8, then count * { pin u8|mode u8|level u8|pwm_freq u32|pwm_duty u32 }
      // "active" = configured via the bridge (mode != 0xFF) or PWM-driven.
      static uint8_t buf[SOC_GPIO_PIN_COUNT * 11 + 1];
      uint16_t n = 1;
      uint8_t count = 0;
      for (uint8_t pin = 0; pin < SOC_GPIO_PIN_COUNT; pin++) {
        if (!valid_pin(pin)) continue;
        uint32_t freq = ledcReadFreq(pin);
        if (pin_mode[pin] == 0xFF && freq == 0) continue;  // untouched: skip
        buf[n++] = pin;
        buf[n++] = pin_mode[pin];
        buf[n++] = (uint8_t)digitalRead(pin);
        wr32(buf + n, freq); n += 4;
        wr32(buf + n, ledcRead(pin)); n += 4;
        count++;
      }
      buf[0] = count;
      proto_reply(seq, cmd, buf, n);
      break;
    }

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}
