// RMT: generic pulse-train play/capture (arduino-esp32 3.x HAL). The firmware
// only moves symbols; device protocols (WS2812, IR, DHT, HC-SR04, steppers)
// are implemented host-side in Python on top of these primitives.
// Handlers run on net_task: TX blocks until sent, RECV up to its timeout.
//
// Wire symbol = u16 BE: level<<15 | duration (ticks, 1..0x7FFF). Two symbols
// pack into one hardware rmt_data_t word; an odd trailing symbol leaves the
// second half zero = end marker.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

#if BRIDGE_HAS_RMT

#include <esp32-hal-rmt.h>

struct RmtChan {
  int8_t pin = -1;
  uint8_t dir;          // 0 tx, 1 rx
  uint32_t tick_hz;
  rmt_data_t* loop_buf; // alive while TX_LOOP runs (HW reads it each pass)
};
#define RMT_MAX_CHANS 4
static RmtChan chans[RMT_MAX_CHANS];

static RmtChan* chan_of(uint8_t pin) {
  for (auto& c : chans) if (c.pin == (int8_t)pin) return &c;
  return nullptr;
}

static bool chan_setup(RmtChan* c) {
  // RX gets 2 mem blocks: captures up to ~2x SOC_RMT_MEM_WORDS_PER_CHANNEL
  // words (192+ edges) without DMA — enough for DHT/NEC and most IR remotes.
  return rmtInit(c->pin, c->dir ? RMT_RX_MODE : RMT_TX_MODE,
                 c->dir ? RMT_MEM_NUM_BLOCKS_2 : RMT_MEM_NUM_BLOCKS_1, c->tick_hz)
         && (c->dir || rmtSetEOT(c->pin, 0));  // TX idles low (WS2812/IR)
}

// Pack host u16 symbols into hardware words; returns word count, sums ticks.
static size_t syms_to_words(const uint8_t* p, uint16_t nsyms, rmt_data_t* out,
                            uint32_t* ticks) {
  size_t w = 0;
  for (uint16_t i = 0; i < nsyms; i++) {
    uint16_t s = rd16(p + 2 * i);
    *ticks += s & 0x7FFF;
    if ((i & 1) == 0) {
      out[w].val = 0;
      out[w].duration0 = s & 0x7FFF;
      out[w].level0 = s >> 15;
    } else {
      out[w].duration1 = s & 0x7FFF;
      out[w].level1 = s >> 15;
      w++;
    }
  }
  if (nsyms & 1) w++;
  return w;
}

// One TX_BYTES bit symbol: u32 = first u16 << 16 | second u16.
static rmt_data_t bit_word(uint32_t v) {
  rmt_data_t d;
  d.val = 0;
  d.duration0 = (v >> 16) & 0x7FFF;
  d.level0 = (v >> 31) & 1;
  d.duration1 = v & 0x7FFF;
  d.level1 = (v >> 15) & 1;
  return d;
}

static uint32_t tx_ms(const RmtChan* c, uint32_t ticks) {
  return ticks / (c->tick_hz / 1000) + 50;  // tick_hz >= 1000 enforced at INIT
}

static void handle_tx(RmtChan* c, uint8_t seq, uint16_t cmd,
                      const uint8_t* p, uint16_t len, bool loop) {
  uint16_t nsyms = len / 2;
  if (nsyms == 0) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
  rmt_data_t* buf = (rmt_data_t*)malloc(((nsyms + 1) / 2) * sizeof(rmt_data_t));
  if (!buf) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
  uint32_t ticks = 0;
  size_t w = syms_to_words(p, nsyms, buf, &ticks);
  if (loop) {
    rmtWriteLooping(c->pin, nullptr, 0);  // stop a previous loop first
    free(c->loop_buf);
    if (!rmtWriteLooping(c->pin, buf, w)) {
      free(buf);
      c->loop_buf = nullptr;
      proto_reply_err(seq, cmd, ST_IO);
      return;
    }
    c->loop_buf = buf;
    proto_reply_ok(seq, cmd);
  } else {
    bool ok = rmtWrite(c->pin, buf, w, tx_ms(c, ticks));
    free(buf);
    ok ? proto_reply_ok(seq, cmd) : proto_reply_err(seq, cmd, ST_IO);
  }
}

// Expand data bytes (MSB-first) into per-bit symbol words and transmit.
// Whole-buffer path keeps the pulse train gapless (WS2812 strips); if the
// heap can't hold it, fall back to chunks through a small static stage —
// chunk boundaries may add a few-us gap (harmless for IR, risky for LEDs).
#define RMT_STAGE_WORDS 256
static void handle_tx_bytes(RmtChan* c, uint8_t seq, uint16_t cmd,
                            const uint8_t* p, uint16_t len) {
  if (len < 9) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
  rmt_data_t w0 = bit_word(rd32(p)), w1 = bit_word(rd32(p + 4));
  const uint8_t* data = p + 8;
  uint32_t nbits = (uint32_t)(len - 8) * 8;
  uint32_t bit_ticks = w0.duration0 + w0.duration1;  // per-bit period (use bit0)
  rmt_data_t* buf = (rmt_data_t*)malloc(nbits * sizeof(rmt_data_t));
  if (buf) {
    size_t w = 0;
    for (uint16_t i = 0; i < len - 8; i++)
      for (int8_t b = 7; b >= 0; b--) buf[w++] = (data[i] >> b) & 1 ? w1 : w0;
    bool ok = rmtWrite(c->pin, buf, w, tx_ms(c, nbits * bit_ticks));
    free(buf);
    ok ? proto_reply_ok(seq, cmd) : proto_reply_err(seq, cmd, ST_IO);
    return;
  }
  static rmt_data_t stage[RMT_STAGE_WORDS];
  size_t w = 0;
  for (uint16_t i = 0; i < len - 8; i++) {
    for (int8_t b = 7; b >= 0; b--) {
      stage[w++] = (data[i] >> b) & 1 ? w1 : w0;
      if (w == RMT_STAGE_WORDS) {
        if (!rmtWrite(c->pin, stage, w, tx_ms(c, w * 2 * bit_ticks)))
          { proto_reply_err(seq, cmd, ST_IO); return; }
        w = 0;
      }
    }
  }
  if (w && !rmtWrite(c->pin, stage, w, tx_ms(c, w * 2 * bit_ticks)))
    { proto_reply_err(seq, cmd, ST_IO); return; }
  proto_reply_ok(seq, cmd);
}

// Arm RX, optionally fire a trigger pulse (same pin open-drain = DHT start
// signal; separate pin = HC-SR04 trigger), wait for capture or timeout.
// Replies the captured symbols (empty payload = nothing seen).
static void handle_recv(RmtChan* c, uint8_t seq, uint16_t cmd,
                        const uint8_t* p, uint16_t len) {
  if (len < 13) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
  uint16_t idle = rd16(p + 1), timeout = rd16(p + 3), max_syms = rd16(p + 5);
  uint8_t trig_pin = p[7], trig_level = p[8];
  uint32_t trig_us = rd32(p + 9);
  if (max_syms == 0 || max_syms > RMT_MAX_RX_SYMS) max_syms = RMT_MAX_RX_SYMS;

  size_t words = (max_syms + 1) / 2;
  rmt_data_t* buf = (rmt_data_t*)malloc(words * sizeof(rmt_data_t));
  if (!buf) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
  rmtSetRxMaxThreshold(c->pin, idle);
  size_t num = words;
  if (!rmtReadAsync(c->pin, buf, &num)) {
    // channel left armed by a timed-out RECV: reset and retry once
    rmtDeinit(c->pin);
    num = words;
    if (!chan_setup(c) || !rmtSetRxMaxThreshold(c->pin, idle)
        || !rmtReadAsync(c->pin, buf, &num)) {
      free(buf);
      proto_reply_err(seq, cmd, ST_IO);
      return;
    }
  }

  if (trig_pin != 0xFF) {
    // Same-pin trigger uses open drain so the RX input stays connected and
    // the line releases high (DHT). Separate pins drive push-pull (HC-SR04).
    pinMode(trig_pin, trig_pin == (uint8_t)c->pin ? OUTPUT_OPEN_DRAIN : OUTPUT);
    digitalWrite(trig_pin, trig_level);
    if (trig_us >= 1000) delay(trig_us / 1000);
    delayMicroseconds(trig_us % 1000);
    digitalWrite(trig_pin, !trig_level);
  }

  uint32_t t0 = millis();
  while (!rmtReceiveCompleted(c->pin) && millis() - t0 < timeout) vTaskDelay(1);

  if (!rmtReceiveCompleted(c->pin)) {
    rmtDeinit(c->pin);  // disarm; next op re-inits
    chan_setup(c);
    free(buf);
    proto_reply(seq, cmd, nullptr, 0);
    return;
  }
  // Pack halves u16 BE in place (each word reads 4 bytes, writes 4 bytes).
  uint8_t* out = (uint8_t*)buf;
  uint16_t halves = 0;
  for (size_t i = 0; i < num; i++) {
    uint16_t h0 = (buf[i].level0 << 15) | buf[i].duration0;
    uint16_t h1 = (buf[i].level1 << 15) | buf[i].duration1;
    wr16(out + 4 * i, h0);
    wr16(out + 4 * i + 2, h1);
    halves += 2;
  }
  while (halves && (rd16(out + 2 * (halves - 1)) & 0x7FFF) == 0) halves--;  // trim end markers
  proto_reply(seq, cmd, out, halves * 2);
  free(buf);
}

void rmt_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_RMT, op);
  if (op == 0x01) {  // INIT: pin|dir|tick_hz
    if (len < 6 || p[1] > 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
    uint32_t hz = rd32(p + 2);
    if (hz < 1000) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
    RmtChan* c = chan_of(p[0]);
    if (c) { rmtDeinit(c->pin); free(c->loop_buf); c->loop_buf = nullptr; }
    else c = chan_of(0xFF);  // unused slot (pin -1)
    if (!c) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
    c->pin = p[0]; c->dir = p[1]; c->tick_hz = hz;
    if (!chan_setup(c)) { c->pin = -1; proto_reply_err(seq, cmd, ST_IO); return; }
    proto_reply_ok(seq, cmd);
    return;
  }
  RmtChan* c = len >= 1 ? chan_of(p[0]) : nullptr;
  if (!c) { proto_reply_err(seq, cmd, len ? ST_NOT_INIT : ST_BAD_ARGS); return; }
  switch (op) {
    case 0x02:  // DEINIT: pin
      rmtWriteLooping(c->pin, nullptr, 0);
      rmtDeinit(c->pin);
      free(c->loop_buf);
      c->loop_buf = nullptr;
      c->pin = -1;
      proto_reply_ok(seq, cmd);
      break;
    case 0x03: handle_tx(c, seq, cmd, p + 1, len - 1, false); break;  // TX
    case 0x04: handle_tx_bytes(c, seq, cmd, p + 1, len - 1); break;   // TX_BYTES
    case 0x05: handle_tx(c, seq, cmd, p + 1, len - 1, true); break;   // TX_LOOP
    case 0x06:  // TX_STOP
      rmtWriteLooping(c->pin, nullptr, 0);
      free(c->loop_buf);
      c->loop_buf = nullptr;
      proto_reply_ok(seq, cmd);
      break;
    case 0x07: handle_recv(c, seq, cmd, p, len); break;  // RECV
    case 0x08:  // CARRIER: pin|freq u32|duty_pct u8|enable u8
      if (len < 7) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (!rmtSetCarrier(c->pin, p[6], true, rd32(p + 1), p[5] / 100.0f))
        { proto_reply_err(seq, cmd, ST_IO); return; }
      proto_reply_ok(seq, cmd);
      break;
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#else  // !BRIDGE_HAS_RMT

void rmt_handle(uint8_t, uint8_t seq, const uint8_t*, uint16_t) {
  proto_reply_err(seq, CMD(MOD_RMT, 0), ST_UNSUPPORTED);
}

#endif
