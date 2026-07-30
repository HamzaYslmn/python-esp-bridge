// python-esp-bridge — COBS framing, CRC16, FreeRTOS tasks, dispatch.
// Architecture-independent: the ESP/nRF specifics live behind platform.h
// (plat_*) and the per-arch link layer. proto_log_hook_install() is defined
// per arch (src/esp/plat_esp.cpp, src/nrf/plat_nrf.cpp).
#include "protocol.h"
#include "modules.h"
#include "link.h"
#include "platform.h"
#if defined(ARDUINO_ARCH_NRF52)
// On arduino-esp32 the FreeRTOS API arrives transitively via Arduino.h; the
// Bluefruit core needs the queue/task headers pulled in explicitly.
#include <FreeRTOS.h>
#include <task.h>
#include <queue.h>
#endif
#include <atomic>  // inter-task counters/flags: atomic is the correct primitive (volatile is not)
#include <new>     // nothrow allocation of the on-demand Wi-Fi link buffers

// ---- CRC-16/CCITT-FALSE (table-driven) --------------------------------------
// 256-entry table (512 B rodata) replaces the per-byte bit loop on the hot path.
// poly=0x1021, init=0xFFFF, no reflection — matches Python binascii.crc_hqx(_, 0xFFFF).
static const uint16_t crc16_table[256] = {
  0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7,
  0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF,
  0x1231, 0x0210, 0x3273, 0x2252, 0x52B5, 0x4294, 0x72F7, 0x62D6,
  0x9339, 0x8318, 0xB37B, 0xA35A, 0xD3BD, 0xC39C, 0xF3FF, 0xE3DE,
  0x2462, 0x3443, 0x0420, 0x1401, 0x64E6, 0x74C7, 0x44A4, 0x5485,
  0xA56A, 0xB54B, 0x8528, 0x9509, 0xE5EE, 0xF5CF, 0xC5AC, 0xD58D,
  0x3653, 0x2672, 0x1611, 0x0630, 0x76D7, 0x66F6, 0x5695, 0x46B4,
  0xB75B, 0xA77A, 0x9719, 0x8738, 0xF7DF, 0xE7FE, 0xD79D, 0xC7BC,
  0x48C4, 0x58E5, 0x6886, 0x78A7, 0x0840, 0x1861, 0x2802, 0x3823,
  0xC9CC, 0xD9ED, 0xE98E, 0xF9AF, 0x8948, 0x9969, 0xA90A, 0xB92B,
  0x5AF5, 0x4AD4, 0x7AB7, 0x6A96, 0x1A71, 0x0A50, 0x3A33, 0x2A12,
  0xDBFD, 0xCBDC, 0xFBBF, 0xEB9E, 0x9B79, 0x8B58, 0xBB3B, 0xAB1A,
  0x6CA6, 0x7C87, 0x4CE4, 0x5CC5, 0x2C22, 0x3C03, 0x0C60, 0x1C41,
  0xEDAE, 0xFD8F, 0xCDEC, 0xDDCD, 0xAD2A, 0xBD0B, 0x8D68, 0x9D49,
  0x7E97, 0x6EB6, 0x5ED5, 0x4EF4, 0x3E13, 0x2E32, 0x1E51, 0x0E70,
  0xFF9F, 0xEFBE, 0xDFDD, 0xCFFC, 0xBF1B, 0xAF3A, 0x9F59, 0x8F78,
  0x9188, 0x81A9, 0xB1CA, 0xA1EB, 0xD10C, 0xC12D, 0xF14E, 0xE16F,
  0x1080, 0x00A1, 0x30C2, 0x20E3, 0x5004, 0x4025, 0x7046, 0x6067,
  0x83B9, 0x9398, 0xA3FB, 0xB3DA, 0xC33D, 0xD31C, 0xE37F, 0xF35E,
  0x02B1, 0x1290, 0x22F3, 0x32D2, 0x4235, 0x5214, 0x6277, 0x7256,
  0xB5EA, 0xA5CB, 0x95A8, 0x8589, 0xF56E, 0xE54F, 0xD52C, 0xC50D,
  0x34E2, 0x24C3, 0x14A0, 0x0481, 0x7466, 0x6447, 0x5424, 0x4405,
  0xA7DB, 0xB7FA, 0x8799, 0x97B8, 0xE75F, 0xF77E, 0xC71D, 0xD73C,
  0x26D3, 0x36F2, 0x0691, 0x16B0, 0x6657, 0x7676, 0x4615, 0x5634,
  0xD94C, 0xC96D, 0xF90E, 0xE92F, 0x99C8, 0x89E9, 0xB98A, 0xA9AB,
  0x5844, 0x4865, 0x7806, 0x6827, 0x18C0, 0x08E1, 0x3882, 0x28A3,
  0xCB7D, 0xDB5C, 0xEB3F, 0xFB1E, 0x8BF9, 0x9BD8, 0xABBB, 0xBB9A,
  0x4A75, 0x5A54, 0x6A37, 0x7A16, 0x0AF1, 0x1AD0, 0x2AB3, 0x3A92,
  0xFD2E, 0xED0F, 0xDD6C, 0xCD4D, 0xBDAA, 0xAD8B, 0x9DE8, 0x8DC9,
  0x7C26, 0x6C07, 0x5C64, 0x4C45, 0x3CA2, 0x2C83, 0x1CE0, 0x0CC1,
  0xEF1F, 0xFF3E, 0xCF5D, 0xDF7C, 0xAF9B, 0xBFBA, 0x8FD9, 0x9FF8,
  0x6E17, 0x7E36, 0x4E55, 0x5E74, 0x2E93, 0x3EB2, 0x0ED1, 0x1EF0,
};

// Incremental form: feed any number of pieces, starting from 0xFFFF.
static uint16_t crc16_update(uint16_t crc, const uint8_t* data, uint16_t len) {
  for (uint16_t i = 0; i < len; i++)
    crc = (uint16_t)(crc << 8) ^ crc16_table[(uint8_t)((crc >> 8) ^ data[i])];
  return crc;
}

uint16_t crc16_ccitt(const uint8_t* data, uint16_t len) {
  return crc16_update(0xFFFF, data, len);
}

// ---- COBS (Consistent Overhead Byte Stuffing) -------------------------------
// Incremental encoder: header/payload/CRC fed straight to the output in pieces,
// so no 2 KB scratch frame. out needs len + len/254 + 1 bytes (worst case).
struct CobsEnc {
  uint8_t* out;
  uint16_t wi;      // next write index
  uint16_t code_i;  // index of the pending code byte
  uint8_t code;
};

static void cobs_enc_begin(CobsEnc& e, uint8_t* out) {
  e.out = out;
  e.wi = 1;
  e.code_i = 0;
  e.code = 1;
}

static void cobs_enc_feed(CobsEnc& e, const uint8_t* d, uint16_t n) {
  for (uint16_t i = 0; i < n; i++) {
    if (d[i] == 0) {
      e.out[e.code_i] = e.code;
      e.code = 1;
      e.code_i = e.wi++;
    } else {
      e.out[e.wi++] = d[i];
      if (++e.code == 0xFF) {
        e.out[e.code_i] = e.code;
        e.code = 1;
        e.code_i = e.wi++;
      }
    }
  }
}

static uint16_t cobs_enc_finish(CobsEnc& e) {
  e.out[e.code_i] = e.code;
  return e.wi;
}

// Decode in to out; returns byte count, or 0 on malformed input. In-place safe
// (out == in): the write cursor never overtakes the read cursor.
static uint16_t cobs_decode(const uint8_t* in, uint16_t len, uint8_t* out) {
  uint16_t ri = 0, wi = 0;
  while (ri < len) {
    uint8_t code = in[ri++];
    if (code == 0 || ri + code - 1 > len) return 0;
    for (uint8_t i = 1; i < code; i++) out[wi++] = in[ri++];
    if (code != 0xFF && ri < len) out[wi++] = 0;
  }
  return wi;
}

// ---- outbound per-link streams + tx task ------------------------------------
// dest controls routing at enqueue time:
//   DEST_ALL  — a copy to every link that is up (serial always; BLE when authed).
//   LINK_USB / LINK_BLE — that link only (replies; the pre-auth SYS_AUTH exchange).
#define DEST_ALL 0xFF

struct Frame { uint8_t flags; uint8_t seq; uint16_t cmd; uint16_t len; uint8_t* buf; };

// One outbound stream per link: a frame queue + a write cursor over the wire
// bytes in flight. tx_task advances both with non-blocking writes only (serial
// gets what fits its TX ring, BLE one MTU chunk when uncongested), so a stalled
// link delays only its own frames — not the other link's, as a single queue did.
struct TxStream {
  QueueHandle_t q;
  uint8_t wire[ENC_BUF_SIZE];   // encoded frame in flight
  volatile uint16_t len;        // 0 = stream idle (polled by proto_tx_flush)
  uint16_t pos;                 // bytes already handed to the link driver
};
static TxStream tx_usb;
#if BRIDGE_BLE
static TxStream tx_ble;        // ~2 KB wire buffer + queue: only on BLE builds
#endif
#if BRIDGE_WIFI_LINK
// Heap, not BSS: allocated by proto_link_tcp_alloc() when the Wi-Fi link is
// actually enabled. nullptr = link absent, which every seam below checks.
static TxStream* tx_net = nullptr;
#endif
static TaskHandle_t tx_task_h;  // enqueue_frame notifies it out of its idle wait
static std::atomic<uint32_t> dropped{0};  // frames dropped across all producer tasks

// Reply routing: a reply returns only on its request's link (broadcasting a 2 KB
// reply into the idle UART during a BLE session stalled tx_task ~180 ms). The
// dispatching task identifies the origin, so a per-task origin set suffices.
static TaskHandle_t rx_task_h, slow_task_h;
static uint8_t origin_rx = DEST_ALL, origin_slow = DEST_ALL;

static uint8_t reply_destination() {
  TaskHandle_t t = xTaskGetCurrentTaskHandle();
  if (t == rx_task_h) return origin_rx;
  if (t == slow_task_h) return origin_slow;
  return DEST_ALL;  // unknown task (shouldn't happen): fall back to broadcast
}

static void enqueue_copy(TxStream& s, uint8_t flags, uint8_t seq, uint16_t cmd,
                       const uint8_t* data, uint16_t len) {
  Frame f = { flags, seq, cmd, len, nullptr };
  if (len) {
    f.buf = (uint8_t*)malloc(len);
    if (!f.buf) { dropped++; return; }
    memcpy(f.buf, data, len);
  }
  // Replies (seq != 0): the host is blocking, waiting for the response, so
  // allow a short wait if the queue is temporarily full.
  // Events (seq == 0): best-effort — drop immediately if the queue is full.
  TickType_t wait = seq != 0 ? pdMS_TO_TICKS(250) : 0;
  if (xQueueSend(s.q, &f, wait) != pdTRUE) {
    free(f.buf);
    dropped++;
  }
}

static void enqueue_frame(uint8_t flags, uint8_t seq, uint16_t cmd,
                          const uint8_t* data, uint16_t len, uint8_t dest = DEST_ALL) {
  if (!tx_usb.q) { dropped++; return; }  // proto_log_hook_install() may call this before proto_init()
  if (len > MAX_PAYLOAD) { dropped++; return; }
  // Each link gets its own copy and drains it at its own pace — a broadcast
  // event to a slow link can back up only that link's queue.
  if (dest == LINK_USB || dest == DEST_ALL)
    enqueue_copy(tx_usb, flags, seq, cmd, data, len);
#if BRIDGE_BLE
  if (dest == LINK_BLE || (dest == DEST_ALL && link_ble_authed()))
    enqueue_copy(tx_ble, flags, seq, cmd, data, len);
#endif
#if BRIDGE_WIFI_LINK
  if (tx_net && (dest == LINK_TCP || (dest == DEST_ALL && link_tcp_authed())))
    enqueue_copy(*tx_net, flags, seq, cmd, data, len);
#endif
  if (tx_task_h) xTaskNotifyGive(tx_task_h);  // wake the drainer (latched if it isn't waiting)
}


// Dequeue the next frame of `s` (if any) and encode it into the stream's wire
// buffer — header, payload and CRC fed incrementally, no scratch copy. tx_task only.
static void load_stream_frame(TxStream& s) {
  Frame f;
  if (xQueueReceive(s.q, &f, 0) != pdTRUE) return;
  uint8_t hdr[4] = { f.flags, f.seq, (uint8_t)(f.cmd >> 8), (uint8_t)f.cmd };
  uint16_t crc = crc16_update(0xFFFF, hdr, 4);
  if (f.len) crc = crc16_update(crc, f.buf, f.len);
  uint8_t tail[2] = { (uint8_t)(crc >> 8), (uint8_t)crc };
  CobsEnc e;
  cobs_enc_begin(e, s.wire);
  cobs_enc_feed(e, hdr, 4);
  if (f.len) cobs_enc_feed(e, f.buf, f.len);
  free(f.buf);
  cobs_enc_feed(e, tail, 2);
  uint16_t n = cobs_enc_finish(e);
  s.wire[n++] = 0x00;
  s.pos = 0;
  s.len = n;
}

// Advance one stream without blocking; returns true if any bytes moved.
static bool advance_usb_stream() {
  if (tx_usb.len == 0) load_stream_frame(tx_usb);
  if (tx_usb.len == 0) return false;
  int room = Serial.availableForWrite();
  if (room <= 0) return false;  // TX ring full: the UART drains it at line rate
  uint16_t n = tx_usb.len - tx_usb.pos;
  if ((int)n > room) n = (uint16_t)room;
  Serial.write(tx_usb.wire + tx_usb.pos, n);  // fits the ring: never blocks
  tx_usb.pos += n;
  if (tx_usb.pos == tx_usb.len) tx_usb.len = 0;
  return true;
}

// Peer gone: drop the in-flight frame and the queue — undeliverable, and
// holding them would leak into the next session.
static void drop_stream(TxStream& s) {
  s.len = 0;
  Frame f;
  while (xQueueReceive(s.q, &f, 0) == pdTRUE) {
    free(f.buf);
    dropped++;
  }
}

// Both wireless links have the identical shape: chunked writes, 0 bytes means
// "congested, retry next pass", and the peer can vanish mid-frame. Only which
// pair of link functions to call differs — indirect calls cost nothing here,
// once per tx_task pass rather than per byte.
static bool advance_chunked(TxStream& s, bool (*up)(),
                            uint16_t (*write_chunk)(const uint8_t*, uint16_t)) {
  if (s.len == 0) load_stream_frame(s);
  if (s.len == 0) return false;
  if (!up()) { drop_stream(s); return false; }
  uint16_t n = write_chunk(s.wire + s.pos, s.len - s.pos);
  if (n == 0) return false;
  s.pos += n;
  if (s.pos == s.len) s.len = 0;
  return true;
}

static bool advance_ble_stream() {
#if BRIDGE_BLE
  return advance_chunked(tx_ble, link_ble_up, link_ble_write_chunk);
#else
  return false;
#endif
}

static bool advance_net_stream() {
#if BRIDGE_WIFI_LINK
  return tx_net && advance_chunked(*tx_net, link_tcp_up, link_tcp_write_chunk);
#else
  return false;
#endif
}

static bool tx_streams_idle() {
  if (tx_usb.len) return false;
#if BRIDGE_BLE
  if (tx_ble.len) return false;
#endif
#if BRIDGE_WIFI_LINK
  if (tx_net && tx_net->len) return false;
#endif
  return true;
}

static void tx_task(void*) {
  uint16_t spins = 0;
  for (;;) {
    bool moved = advance_usb_stream();
    moved = advance_ble_stream() || moved;
    moved = advance_net_stream() || moved;
    if (!moved) {
      spins = 0;
      if (tx_streams_idle()) {
        // Fully idle: sleep until enqueue_frame notifies (latched, so a
        // notify that races this wait is not lost).
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(100));
      } else {
        vTaskDelay(1);  // a link is stalled (ring full / congested): poll soon
      }
    } else if (++spins >= RX_YIELD_EVERY) {
      spins = 0;
      vTaskDelay(1);  // sustained progress: keep the idle task alive
    }
  }
}

void proto_reply(uint8_t seq, uint16_t cmd, const uint8_t* data, uint16_t len) {
  if (seq == 0) return;  // fire-and-forget: no reply
  enqueue_frame(0, seq, cmd, data, len, reply_destination());
}

void proto_reply_ok(uint8_t seq, uint16_t cmd) { proto_reply(seq, cmd, nullptr, 0); }

void proto_reply_err(uint8_t seq, uint16_t cmd, uint8_t status) {
  if (seq == 0) return;
  enqueue_frame(FLAG_ERROR, seq, cmd, &status, 1, reply_destination());
}

void proto_send_event(uint16_t cmd, const uint8_t* data, uint16_t len) {
  enqueue_frame(FLAG_EVENT, 0, cmd, data, len);
}

void proto_log(uint8_t level, const char* msg) {
  uint8_t buf[1 + 128];
  uint16_t n = strlen(msg);
  if (n > 128) n = 128;
  buf[0] = level;
  memcpy(buf + 1, msg, n);
  proto_send_event(SYS_LOG, buf, 1 + n);
}

void proto_log_heap(const char* stage) {
  char msg[64];
  snprintf(msg, sizeof(msg), "%s, %lu B free heap", stage,
           (unsigned long)plat_free_heap());
  proto_log(1, msg);
}

void proto_tx_flush() {
  // Drain the SERIAL stream only — callers (the baud switch) care about bytes
  // still headed for the UART; the BLE stream is unaffected by a baud change.
  while (uxQueueMessagesWaiting(tx_usb.q) > 0 || tx_usb.len) vTaskDelay(1);
  Serial.flush();
}

uint32_t proto_dropped_events() { return dropped; }

// ---- blocking-handler queue (bridge_slow) -----------------------------------
// WIFI/NET/BLE/etc. handlers can block for seconds (TCP/BLE connect) and share
// state with socket polling, so they run on slow_task (on CORE_RADIO, next to
// the stacks they call) instead of stalling rx_task.
struct Req { uint16_t cmd; uint8_t seq; uint8_t origin; uint16_t len; uint8_t* buf; };
static QueueHandle_t slowq;

static void slow_dispatch(uint16_t cmd, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint8_t mod = cmd >> 8, op = cmd & 0xFF;
  switch (mod) {
    case MOD_SYS:    sys_handle(op, seq, p, len); break;  // only SYS_RADIO_OFF routes here
    case MOD_WIFI:   wifi_handle(op, seq, p, len); break;
    case MOD_NET:    net_handle(op, seq, p, len); break;
    case MOD_ESPNOW: espnow_handle(op, seq, p, len); break;
    case MOD_BLE:    ble_handle(op, seq, p, len); break;
    case MOD_RMT:    rmt_handle(op, seq, p, len); break;
    case MOD_FS:     fs_handle(op, seq, p, len); break;
    case MOD_OTA:    ota_handle(op, seq, p, len); break;
    case MOD_TWAI:   twai_handle(op, seq, p, len); break;
    case MOD_I2S:    i2s_handle(op, seq, p, len); break;
    case MOD_ETH:    eth_handle(op, seq, p, len); break;
    case MOD_CAM:    cam_handle(op, seq, p, len); break;
    case MOD_WATCH:  watch_handle(op, seq, p, len); break;
    default:         proto_reply_err(seq, cmd, ST_UNKNOWN_CMD); break;
  }
}

static void slow_task(void*) {
  Req r;
  uint16_t spins = 0;
  for (;;) {
    // Wake at least every 2 ms to poll sockets / scan completion.
    if (xQueueReceive(slowq, &r, pdMS_TO_TICKS(2)) == pdTRUE) {
      origin_slow = r.origin;  // reply_destination() routes this request's replies
      slow_dispatch(r.cmd, r.seq, r.buf, r.len);
      free(r.buf);
      // A continuously-fed queue never hits the 2 ms timeout above, so yield
      // periodically to keep the idle task (and its Task-WDT) alive.
      if (++spins >= RX_YIELD_EVERY) { spins = 0; vTaskDelay(1); }
    } else {
      spins = 0;  // the 2 ms receive timeout already yielded
    }
    wifi_poll();
    net_poll();
#if BRIDGE_WIFI_LINK
    link_tcp_poll();  // accept / dial-home / reconnect backoff (may block briefly)
#endif
    twai_poll();
    watch_poll();  // polled (non-ISR) user watch rules -> WATCH_EVT
  }
}

static void slow_enqueue(uint16_t cmd, uint8_t seq, uint8_t origin,
                        const uint8_t* p, uint16_t len) {
  Req r = { cmd, seq, origin, len, nullptr };
  if (len) {
    r.buf = (uint8_t*)malloc(len);
    if (!r.buf) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
    memcpy(r.buf, p, len);
  }
  if (xQueueSend(slowq, &r, 0) != pdTRUE) {
    free(r.buf);
    proto_reply_err(seq, cmd, ST_BUSY);
  }
}

// ---- RX & dispatch (rx_task) ------------------------------------------------
// Per-link COBS accumulator: USB and BLE bytes arrive independently and may
// interleave. Frames decode in place here (cobs_decode), so no scratch buffer.
struct RxState {
  uint8_t acc[ENC_BUF_SIZE];
  uint16_t len = 0;
  bool overflow = false;
};
static RxState rxstate[BRIDGE_BLE ? 2 : 1];  // [LINK_USB], [LINK_BLE]
#if BRIDGE_WIFI_LINK
static RxState* rx_net = nullptr;  // heap, like tx_net — see proto_link_tcp_alloc
#endif

static RxState* rx_for(uint8_t origin) {
#if BRIDGE_WIFI_LINK
  if (origin == LINK_TCP) return rx_net;
#endif
  return &rxstate[origin];
}

static void dispatch(uint8_t origin, uint8_t seq, uint16_t cmd,
                     const uint8_t* p, uint16_t len) {
  origin_rx = origin;  // reply_destination() routes inline handlers' replies
  uint8_t mod = cmd >> 8, op = cmd & 0xFF;
  switch (mod) {
    // Fast handlers: run inline on rx_task.
    case MOD_SYS:
      // RADIO_OFF is the one SYS op that must run on slow_task: it tears down
      // the radio stacks next to every other task that calls into them.
      if (op == (SYS_RADIO_OFF & 0xFF)) { slow_enqueue(cmd, seq, origin, p, len); break; }
      sys_handle(op, seq, p, len); break;
    case MOD_GPIO:  gpio_handle(op, seq, p, len); break;
    case MOD_ADC:
    case MOD_DAC:
    case MOD_TOUCH: analog_handle(mod, op, seq, p, len); break;
    case MOD_PWM:   pwm_handle(op, seq, p, len); break;
    case MOD_I2C:   i2c_handle(op, seq, p, len); break;
    case MOD_SPI:   spi_handle(op, seq, p, len); break;
    case MOD_UART:  uart_handle(op, seq, p, len); break;
    case MOD_NVS:   nvs_handle_cmd(op, seq, p, len); break;
    case MOD_MCPWM: mcpwm_handle(op, seq, p, len); break;
    // 1-Wire is inline by necessity: its bit-banged slots mask interrupts up to
    // 70 µs, which must never hit the radio core where slow_task lives.
    case MOD_ONEWIRE: onewire_handle(op, seq, p, len); break;
    // Everything else is slow/stateful (WIFI/NET/ESPNOW/BLE/RMT/FS/OTA/TWAI/I2S/
    // ETH/CAM) — hand to slow_task, which replies ST_UNKNOWN_CMD if unrecognised.
    default:        slow_enqueue(cmd, seq, origin, p, len); break;
  }
}

// ---- per-link auth policy (see link.h) --------------------------------------
// USB is trusted (you are holding the cable); BLE and TCP must pass SYS_AUTH.
bool link_needs_auth(uint8_t link) { return link == LINK_BLE || link == LINK_TCP; }

bool link_authed(uint8_t link) {
  if (link == LINK_BLE) return link_ble_authed();
#if BRIDGE_WIFI_LINK
  if (link == LINK_TCP) return link_tcp_authed();
#endif
  return true;  // USB
}

void link_set_authed(uint8_t link, bool v) {
  if (link == LINK_BLE) link_ble_set_authed(v);
#if BRIDGE_WIFI_LINK
  else if (link == LINK_TCP) link_tcp_set_authed(v);
#endif
}

const char* link_auth_password(uint8_t link) {
#if BRIDGE_WIFI_LINK
  if (link == LINK_TCP) return link_tcp_password();
#endif
  (void)link;
  return link_ble_password();
}

// Constant-time-ish password compare (no early exit on mismatch).
static bool password_ok(uint8_t origin, const uint8_t* p, uint16_t len) {
  const char* pw = link_auth_password(origin);
  uint16_t pwlen = strlen(pw);
  uint8_t diff = (len == pwlen) ? 0 : 1;
  for (uint16_t i = 0; i < len; i++) diff |= p[i] ^ (uint8_t)pw[i % (pwlen ? pwlen : 1)];
  return diff == 0;  // empty BRIDGE_PASSWORD = open access
}

// Runs before normal dispatch because it is a link-layer concern: replies must
// reach a not-yet-authenticated client.
static bool handle_auth(uint8_t origin, uint8_t seq, uint16_t cmd,
                        const uint8_t* p, uint16_t len) {
  bool gated = link_needs_auth(origin);
  uint8_t dest = gated ? origin : DEST_ALL;
  if (cmd == SYS_AUTH) {
    if (!gated) {  // USB implies physical access: always granted
      enqueue_frame(0, seq, cmd, nullptr, 0, dest);
      return true;
    }
    if (password_ok(origin, p, len)) {
      link_set_authed(origin, true);
      enqueue_frame(0, seq, cmd, nullptr, 0, origin);
      // Boot-banner equivalent so the host handshake works like USB.
      uint8_t info[64];
      enqueue_frame(FLAG_EVENT, 0, SYS_READY, info, sys_build_info(info), origin);
      proto_log_heap(origin == LINK_TCP ? "wifi: authed" : "ble: authed");
    } else {
      uint8_t st = ST_DENIED;
      enqueue_frame(FLAG_ERROR, seq, cmd, &st, 1, origin);
    }
    return true;
  }
  if (gated && !link_authed(origin)) {
    uint8_t st = ST_DENIED;
    enqueue_frame(FLAG_ERROR, seq, cmd, &st, 1, origin);
    return true;
  }
  return false;
}

// Corrupt USB frames (bad COBS, short, CRC fail). Counted and exposed via
// SYS_FREE_HEAP so host retries trace to real line corruption, not guesswork.
static uint32_t serial_rx_errors = 0;
uint32_t link_serial_rx_errors() { return serial_rx_errors; }

// Decode in place (no scratch buffer) and dispatch. Safe to refill the
// accumulator on return: inline handlers run to completion, slow_enqueue copies.
static void decode_and_dispatch(uint8_t origin, uint8_t* enc, uint16_t enclen) {
  uint16_t n = cobs_decode(enc, enclen, enc);
  if (n < 6 || crc16_ccitt(enc, n - 2) != read_be16(enc + n - 2)) {
    // Corrupted frame: drop it and let the host retry on timeout.
    if (origin == LINK_USB) serial_rx_errors++;
    return;
  }
  uint8_t seq = enc[1];
  uint16_t cmd = read_be16(enc + 2);
  if (handle_auth(origin, seq, cmd, enc + 4, n - 6)) return;
  dispatch(origin, seq, cmd, enc + 4, n - 6);
}

static void pump_link_bytes(uint8_t origin, const uint8_t* chunk, int n) {
  RxState* rxp = rx_for(origin);
  if (!rxp) return;  // link torn down mid-drain
  RxState& rx = *rxp;
  int i = 0;
  while (i < n) {
    if (chunk[i] == 0x00) {  // frame delimiter
      if (!rx.overflow && rx.len > 0) decode_and_dispatch(origin, rx.acc, rx.len);
      rx.len = 0;
      rx.overflow = false;
      i++;
      continue;
    }
    // Bulk-copy the run up to the next delimiter — the hot RX path at 1.5 Mbaud.
    const uint8_t* z = (const uint8_t*)memchr(chunk + i, 0x00, n - i);
    int run = (z ? (int)(z - chunk) : n) - i;
    if (rx.overflow || run > (int)sizeof(rx.acc) - rx.len) {
      // Accumulator full (lost delimiter merged frames): drop until the next 0x00.
      if (!rx.overflow && origin == LINK_USB) serial_rx_errors++;
      rx.overflow = true;
    } else {
      memcpy(rx.acc + rx.len, chunk + i, run);
      rx.len += run;
    }
    i += run;
  }
}

// Returns true if any bytes were processed; rx_task yields briefly when false.
static bool proto_pump_rx() {
  uint8_t chunk[256];  // rx_task stack (8 KB) — keeps 256 B out of BSS
  bool any = false;
  for (;;) {
    // Read in blocks rather than one byte at a time. Reading only as many
    // bytes as available() reports keeps the call non-blocking on both
    // HardwareSerial (UART) and HWCDC (native USB).
    int avail = Serial.available();
    if (avail <= 0) break;
    if (avail > (int)sizeof(chunk)) avail = sizeof(chunk);
    int n = Serial.read(chunk, avail);
    if (n <= 0) break;
    any = true;
    pump_link_bytes(LINK_USB, chunk, n);
  }
#if BRIDGE_BLE
  for (;;) {
    uint16_t n = link_ble_read(chunk, sizeof(chunk));
    if (n == 0) break;
    any = true;
    pump_link_bytes(LINK_BLE, chunk, n);
  }
#endif
#if BRIDGE_WIFI_LINK
  for (;;) {
    uint16_t n = link_tcp_read(chunk, sizeof(chunk));
    if (n == 0) break;
    any = true;
    pump_link_bytes(LINK_TCP, chunk, n);
  }
#endif
  return any;
}

static void rx_task(void*) {
  uint16_t spins = 0;
  for (;;) {
    bool busy = proto_pump_rx();
    gpio_poll();   // ISR edge queue -> events
    uart_poll();   // secondary UART RX -> events
    // Yield regularly: idle -> sleep a tick; busy -> still yield every
    // RX_YIELD_EVERY loops, or this prio-10 task starves idle and trips the WDT.
    if (!busy) {
      spins = 0;
      vTaskDelay(1);
    } else if (++spins >= RX_YIELD_EVERY) {
      spins = 0;
      vTaskDelay(1);
    }
  }
}

// ---- init / start -----------------------------------------------------------

void proto_init() {
  tx_usb.q = xQueueCreate(TXQ_DEPTH, sizeof(Frame));
#if BRIDGE_BLE
  tx_ble.q = xQueueCreate(TXQ_DEPTH, sizeof(Frame));
#endif
  slowq = xQueueCreate(SLOWQ_DEPTH, sizeof(Req));
}

#if BRIDGE_WIFI_LINK
// ~4.5 KB on the heap, charged only when the Wi-Fi link is on. See protocol.h.
bool proto_link_tcp_alloc() {
  if (!tx_net) {
    TxStream* s = new (std::nothrow) TxStream();
    if (!s) return false;
    s->q = xQueueCreate(TXQ_DEPTH, sizeof(Frame));
    if (!s->q) { delete s; return false; }
    tx_net = s;
  }
  if (!rx_net) {
    rx_net = new (std::nothrow) RxState();
    if (!rx_net) return false;
  }
  return true;
}

void proto_link_tcp_reset() {
  if (rx_net) { rx_net->len = 0; rx_net->overflow = false; }
  if (tx_net) drop_stream(*tx_net);
}

void proto_link_tcp_free() {
  TxStream* s = tx_net;
  RxState* r = rx_net;
  tx_net = nullptr;
  rx_net = nullptr;
  // A task may be one instruction inside a seam that just read the pointer;
  // give it a couple of ticks to leave before the memory goes away.
  vTaskDelay(2);
  if (s) { drop_stream(*s); vQueueDelete(s->q); delete s; }
  delete r;
}
#endif  // BRIDGE_WIFI_LINK

void proto_start() {
  // Both cores on a dual-core chip (task-layout note in config.h): TX + blocking
  // handlers on the radio core, RX + fast handlers on the app core, so reply N
  // transmits while command N+1 executes.
  xTaskCreatePinnedToCore(tx_task, "bridge_tx", TX_TASK_STACK, nullptr,
                          TX_TASK_PRIO, &tx_task_h, TX_TASK_CORE);
  xTaskCreatePinnedToCore(rx_task, "bridge_rx", RX_TASK_STACK, nullptr,
                          RX_TASK_PRIO, &rx_task_h, RX_TASK_CORE);
  xTaskCreatePinnedToCore(slow_task, "bridge_slow", SLOW_TASK_STACK, nullptr,
                          SLOW_TASK_PRIO, &slow_task_h, SLOW_TASK_CORE);
}
