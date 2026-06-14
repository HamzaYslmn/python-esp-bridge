// GPIO (nRF52): modes, read/write, batch ops. Edge-watch (GPIO_WATCH) is not
// implemented on this build and replies ST_UNSUPPORTED. Counterpart to
// src/esp/mod_gpio.cpp.
#if defined(ARDUINO_ARCH_NRF52)
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

#ifdef PINS_COUNT
  #define BRIDGE_PIN_COUNT PINS_COUNT
#else
  #define BRIDGE_PIN_COUNT 48
#endif

static uint8_t pin_mode[BRIDGE_PIN_COUNT];  // last mode set via SET_MODE (0xFF = unknown)

static bool is_valid_pin(uint8_t pin) { return pin < BRIDGE_PIN_COUNT; }

// ---- edge watch -------------------------------------------------------------
// The nRF core exposes attachInterrupt(pin, fn, mode) but NOT the *Arg variant,
// so each watchable pin needs its own no-arg ISR. A small fixed slot table with
// one trampoline per slot maps back to the pin (GPIOTE has 8 channels anyway).
#define MAX_WATCH 8
struct EdgeRaw { uint8_t pin; uint8_t level; uint32_t ms; };
static QueueHandle_t edgeq;
struct WatchSlot { bool used; uint8_t pin; uint16_t debounce; uint32_t last_ms; };
static WatchSlot slots[MAX_WATCH];

static void edge_common(uint8_t s) {
  if (!slots[s].used) return;
  EdgeRaw e = { slots[s].pin, (uint8_t)digitalRead(slots[s].pin), millis() };
  BaseType_t hpw = pdFALSE;
  xQueueSendFromISR(edgeq, &e, &hpw);
  portYIELD_FROM_ISR(hpw);  // vanilla FreeRTOS: macro takes the woken flag
}
static void tramp0() { edge_common(0); }
static void tramp1() { edge_common(1); }
static void tramp2() { edge_common(2); }
static void tramp3() { edge_common(3); }
static void tramp4() { edge_common(4); }
static void tramp5() { edge_common(5); }
static void tramp6() { edge_common(6); }
static void tramp7() { edge_common(7); }
static void (*const tramps[MAX_WATCH])() = {
  tramp0, tramp1, tramp2, tramp3, tramp4, tramp5, tramp6, tramp7 };

static int slot_of_pin(uint8_t pin) {
  for (int i = 0; i < MAX_WATCH; i++) if (slots[i].used && slots[i].pin == pin) return i;
  return -1;
}

void gpio_init() {
  edgeq = xQueueCreate(64, sizeof(EdgeRaw));
  memset(pin_mode, 0xFF, sizeof(pin_mode));  // 0xFF = mode not set via the bridge
}

void gpio_poll() {
  EdgeRaw e;
  while (xQueueReceive(edgeq, &e, 0) == pdTRUE) {
    int s = slot_of_pin(e.pin);
    if (s < 0) continue;
    uint16_t db = slots[s].debounce;
    if (db && (uint32_t)(e.ms - slots[s].last_ms) < db) continue;
    slots[s].last_ms = e.ms;
    uint8_t buf[6];
    buf[0] = e.pin;
    buf[1] = e.level;
    write_be32(buf + 2, e.ms);
    proto_send_event(GPIO_EDGE_EVT, buf, 6);
  }
}

void gpio_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_GPIO, op);
  switch (op) {
    case 0x01: {  // SET_MODE: pin, mode
      NEED(2);
      uint8_t pin = p[0], mode = p[1];
      if (!is_valid_pin(pin)) { proto_reply_err(seq, cmd, ST_BAD_PIN); return; }
      switch (mode) {
        case 0: pinMode(pin, INPUT); break;
        case 1: pinMode(pin, OUTPUT); break;
        case 2: pinMode(pin, INPUT_PULLUP); break;
        case 3: pinMode(pin, INPUT_PULLDOWN); break;
        case 4:
#ifdef OUTPUT_OPEN_DRAIN
          pinMode(pin, OUTPUT_OPEN_DRAIN); break;
#else
          proto_reply_err(seq, cmd, ST_UNSUPPORTED); return;
#endif
        default: proto_reply_err(seq, cmd, ST_BAD_ARGS); return;
      }
      pin_mode[pin] = mode;
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x02: {  // WRITE: pin, value -> level read back
      NEED(2);
      if (!is_valid_pin(p[0])) { proto_reply_err(seq, cmd, ST_BAD_PIN); return; }
      digitalWrite(p[0], p[1] ? HIGH : LOW);
      uint8_t v = (uint8_t)digitalRead(p[0]);
      proto_reply(seq, cmd, &v, 1);
      break;
    }

    case 0x03: {  // READ: pin -> value
      NEED(1);
      if (!is_valid_pin(p[0])) { proto_reply_err(seq, cmd, ST_BAD_PIN); return; }
      uint8_t v = (uint8_t)digitalRead(p[0]);
      proto_reply(seq, cmd, &v, 1);
      break;
    }

    case 0x04: {  // WRITE_MASK: mask u64, values u64
      NEED(16);
      uint64_t mask = ((uint64_t)read_be32(p) << 32) | read_be32(p + 4);
      uint64_t vals = ((uint64_t)read_be32(p + 8) << 32) | read_be32(p + 12);
      for (uint8_t pin = 0; pin < BRIDGE_PIN_COUNT && pin < 64; pin++)
        if ((mask >> pin) & 1 && is_valid_pin(pin))
          digitalWrite(pin, ((vals >> pin) & 1) ? HIGH : LOW);
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x05: {  // READ_ALL -> levels u64
      uint64_t levels = 0;
      for (uint8_t pin = 0; pin < BRIDGE_PIN_COUNT && pin < 64; pin++)
        if (is_valid_pin(pin) && digitalRead(pin)) levels |= (1ULL << pin);
      uint8_t buf[8];
      write_be32(buf, (uint32_t)(levels >> 32));
      write_be32(buf + 4, (uint32_t)levels);
      proto_reply(seq, cmd, buf, 8);
      break;
    }

    case 0x06: {  // WATCH: pin, edge u8 (1 rise,2 fall,3 change), debounce_ms u16
      NEED(4);
      uint8_t pin = p[0], edge = p[1];
      if (!is_valid_pin(pin)) { proto_reply_err(seq, cmd, ST_BAD_PIN); return; }
      int mode = edge == 1 ? RISING : edge == 2 ? FALLING : edge == 3 ? CHANGE : -1;
      if (mode < 0) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      int s = slot_of_pin(pin);
      if (s < 0) for (int i = 0; i < MAX_WATCH; i++) if (!slots[i].used) { s = i; break; }
      if (s < 0) { proto_reply_err(seq, cmd, ST_BUSY); return; }  // all GPIOTE slots taken
      slots[s] = { true, pin, read_be16(p + 2), 0 };
      attachInterrupt(digitalPinToInterrupt(pin), tramps[s], mode);
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x07: {  // UNWATCH: pin
      NEED(1);
      if (!is_valid_pin(p[0])) { proto_reply_err(seq, cmd, ST_BAD_PIN); return; }
      int s = slot_of_pin(p[0]);
      detachInterrupt(digitalPinToInterrupt(p[0]));
      if (s >= 0) slots[s].used = false;
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x08: {  // STATUS: pin -> level u8|mode u8|pwm_freq u32|pwm_duty u32
      NEED(1);
      uint8_t pin = p[0];
      if (!is_valid_pin(pin)) { proto_reply_err(seq, cmd, ST_BAD_PIN); return; }
      uint8_t buf[10];
      buf[0] = (uint8_t)digitalRead(pin);
      buf[1] = pin_mode[pin];
      write_be32(buf + 2, 0);  // PWM state not tracked per-pin here (see mod_pwm)
      write_be32(buf + 6, 0);
      proto_reply(seq, cmd, buf, 10);
      break;
    }

    case 0x09: {  // DUMP: every bridge-configured pin
      uint8_t buf[BRIDGE_PIN_COUNT * 11 + 1];
      uint16_t n = 1;
      uint8_t count = 0;
      for (uint8_t pin = 0; pin < BRIDGE_PIN_COUNT; pin++) {
        if (pin_mode[pin] == 0xFF) continue;  // untouched: skip
        buf[n++] = pin;
        buf[n++] = pin_mode[pin];
        buf[n++] = (uint8_t)digitalRead(pin);
        write_be32(buf + n, 0); n += 4;
        write_be32(buf + n, 0); n += 4;
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

#endif  // ARDUINO_ARCH_NRF52
