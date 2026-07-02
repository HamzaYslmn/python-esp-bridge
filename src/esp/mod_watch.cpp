#if defined(ARDUINO_ARCH_ESP32)
// WATCH — on-device, POLLED user-definable event engine. The host defines rules
// ("sample ADC pin 4 every 50 ms; fire above 3200"); watch_poll() samples each on
// slow_task and emits WATCH_EVT only on state change. Polled (never in an ISR, so
// the main loop is never interrupted) and single-task (no mutex on the rule table).
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/radio.h"
#include <esp32-hal-adc.h>
#include <esp32-hal-gpio.h>
#include <esp32-hal-ledc.h>
#include <esp_timer.h>

#if BRIDGE_HAS_TOUCH
#include <esp32-hal-touch.h>
#endif

#define WATCH_MAX 16

// sources
enum { SRC_ADC_RAW = 0, SRC_ADC_MV = 1, SRC_GPIO = 2, SRC_TOUCH = 3, SRC_HEAP = 4 };
// comparators
enum { CMP_ABOVE = 0, CMP_BELOW = 1, CMP_INSIDE = 2, CMP_OUTSIDE = 3, CMP_CHANGED = 4 };
// flags
enum { F_ENTER = 0x01, F_EXIT = 0x02, F_INITIAL = 0x04 };
// on-device actions (WATCH_ADD2): executed here on state change, no host round trip
enum { ACT_NONE = 0, ACT_GPIO = 1, ACT_PWM = 2 };

struct WatchAction {
  uint8_t type = ACT_NONE;
  uint8_t pin;
  int32_t value;
};

struct Watch {
  bool     active = false;   // slot in use
  uint8_t  id;
  uint8_t  source;
  uint8_t  arg;              // pin (ADC/GPIO/TOUCH)
  uint8_t  cmp;
  uint8_t  flags;
  uint16_t period_ms;
  int32_t  a, b;
  uint32_t last_ms;
  uint8_t  state;            // 1 = condition active, 0 = inactive
  int32_t  last_val;         // last sample (reported by WATCH_LIST)
  int32_t  last_emit;        // last value emitted (CMP_CHANGED reference)
  bool     primed;           // has taken its first sample
  WatchAction act[2];        // [1] runs on enter/changed, [0] on exit
};

static Watch watches[WATCH_MAX];

static inline uint32_t uptime_ms() { return (uint32_t)(esp_timer_get_time() / 1000ULL); }

static Watch* find_watch(uint8_t id) {
  for (auto& w : watches) if (w.active && w.id == id) return &w;
  return nullptr;
}

// Read a rule's source; false if unreadable now (e.g. ADC2 while Wi-Fi owns the
// ADC — see pin_is_adc2 in modules.h), so the caller skips this tick.
static bool read_watch_source(const Watch& w, int32_t& out) {
  switch (w.source) {
    case SRC_ADC_RAW:
    case SRC_ADC_MV:
      if (pin_is_adc2(w.arg) && radio_active()) return false;
      out = (w.source == SRC_ADC_RAW) ? (int32_t)analogRead(w.arg)
                                      : (int32_t)analogReadMilliVolts(w.arg);
      return true;
    case SRC_GPIO:
      out = digitalRead(w.arg) ? 1 : 0;
      return true;
#if BRIDGE_HAS_TOUCH
    case SRC_TOUCH:
      out = (int32_t)touchRead(w.arg);
      return true;
#endif
    case SRC_HEAP:
      out = (int32_t)ESP.getFreeHeap();
      return true;
    default:
      return false;
  }
}

// Threshold state for the comparators that have an active/inactive notion,
// applying hysteresis (a = engage, b = release) for above/below.
static uint8_t threshold_active(const Watch& w, int32_t v) {
  switch (w.cmp) {
    // Release on a strict inequality so a degenerate band (b==a, i.e. no
    // hysteresis) doesn't oscillate when the value sits exactly on the
    // threshold — important for digital sources (above=1 on a 0/1 GPIO).
    case CMP_ABOVE:   return w.state ? (v <  w.b ? 0 : 1) : (v >= w.a ? 1 : 0);
    case CMP_BELOW:   return w.state ? (v >  w.b ? 0 : 1) : (v <= w.a ? 1 : 0);
    case CMP_INSIDE:  return (v >= w.a && v <= w.b) ? 1 : 0;
    case CMP_OUTSIDE: return (v < w.a || v > w.b) ? 1 : 0;
    default:          return 0;
  }
}

// Run the rule's on-device action for the new state. GPIO pins were set to
// OUTPUT at add time; PWM pins must have been attached by the host (PWM_ATTACH)
// beforehand — ledcWrite on an unattached pin is a no-op, not a crash.
static void run_watch_action(const Watch& w, uint8_t state) {
  const WatchAction& a = w.act[state ? 1 : 0];
  switch (a.type) {
    case ACT_GPIO: digitalWrite(a.pin, a.value ? HIGH : LOW); break;
    case ACT_PWM:  ledcWrite(a.pin, (uint32_t)a.value); break;
  }
}

static void emit_watch_event(const Watch& w, uint8_t state, int32_t value) {
  uint8_t buf[10];
  buf[0] = w.id;
  buf[1] = state;
  write_be32(buf + 2, (uint32_t)value);
  write_be32(buf + 6, uptime_ms());
  proto_send_event(WATCH_EVT, buf, 10);
}

void watch_poll() {
  uint32_t t = uptime_ms();
  for (auto& w : watches) {
    if (!w.active) continue;
    if (w.primed && (uint32_t)(t - w.last_ms) < w.period_ms) continue;

    int32_t v;
    if (!read_watch_source(w, v)) { w.last_ms = t; continue; }  // unreadable now; retry next tick
    w.last_ms = t;
    w.last_val = v;

    if (w.cmp == CMP_CHANGED) {
      if (!w.primed) {
        w.primed = true; w.last_emit = v;
        if (w.flags & F_INITIAL) emit_watch_event(w, 1, v);
      } else {
        int32_t d = v - w.last_emit;
        if (d < 0) d = -d;
        if (d >= w.a) { w.last_emit = v; run_watch_action(w, 1); emit_watch_event(w, 1, v); }
      }
      continue;
    }

    uint8_t ns = threshold_active(w, v);
    if (!w.primed) {
      w.primed = true;
      w.state = ns;
      // The action always tracks the first sample (an output must reflect
      // reality from the start); the F_INITIAL flag only gates the host event.
      run_watch_action(w, ns);
      if (w.flags & F_INITIAL) emit_watch_event(w, ns, v);  // report current state at arm time
      continue;
    }
    if (ns != w.state) {
      w.state = ns;
      run_watch_action(w, ns);  // action first: the reflex beats the report
      if ((ns && (w.flags & F_ENTER)) || (!ns && (w.flags & F_EXIT))) emit_watch_event(w, ns, v);
    }
  }
}

void watch_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_WATCH, op);
  switch (op) {
    case 0x01:    // ADD:  id|source|arg|aux|cmp|flags|period u16|a i32|b i32
    case 0x05: {  // ADD2: ADD + enter/exit actions, each type u8|pin u8|value i32
      bool with_actions = (op == 0x05);
      NEED(with_actions ? 28 : 16);
      uint8_t id = p[0], source = p[1], arg = p[2], aux = p[3], cmp = p[4];
      if (source > SRC_HEAP || cmp > CMP_CHANGED) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
#if !BRIDGE_HAS_TOUCH
      if (source == SRC_TOUCH) { proto_reply_err(seq, cmd, ST_UNSUPPORTED); return; }
#endif
      WatchAction acts[2] = {};  // [1] = enter (first on the wire), [0] = exit
      if (with_actions) {
        for (int i = 0; i < 2; i++) {
          const uint8_t* q = p + 16 + i * 6;
          if (q[0] > ACT_PWM) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
          WatchAction& a = acts[i == 0 ? 1 : 0];
          a.type = q[0]; a.pin = q[1]; a.value = (int32_t)read_be32(q + 2);
          if (a.type == ACT_GPIO) pinMode(a.pin, OUTPUT);
        }
      }
      Watch* w = find_watch(id);
      if (!w) for (auto& s : watches) if (!s.active) { w = &s; break; }
      if (!w) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }  // table full

      if ((source == SRC_ADC_RAW || source == SRC_ADC_MV) && aux <= 3)
        analogSetPinAttenuation(arg, (adc_attenuation_t)aux);

      w->active = true;
      w->id = id; w->source = source; w->arg = arg;
      w->cmp = cmp; w->flags = p[5];
      w->period_ms = read_be16(p + 6); if (w->period_ms == 0) w->period_ms = 100;
      w->a = (int32_t)read_be32(p + 8);
      w->b = (int32_t)read_be32(p + 12);
      w->act[0] = acts[0]; w->act[1] = acts[1];
      w->last_ms = 0; w->state = 0; w->primed = false; w->last_val = 0; w->last_emit = 0;
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x02: {  // REMOVE: id
      NEED(1);
      Watch* w = find_watch(p[0]);
      if (w) w->active = false;
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x03:    // CLEAR
      for (auto& w : watches) w.active = false;
      proto_reply_ok(seq, cmd);
      break;
    case 0x04: {  // LIST -> n|{id|state|value i32}*n
      uint8_t buf[1 + WATCH_MAX * 6];
      uint16_t n = 1; uint8_t count = 0;
      for (auto& w : watches) {
        if (!w.active) continue;
        buf[n++] = w.id;
        buf[n++] = w.state;
        write_be32(buf + n, (uint32_t)w.last_val); n += 4;
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
#endif  // ARDUINO_ARCH_ESP32
