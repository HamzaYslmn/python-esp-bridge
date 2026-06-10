// python-esp-bridge — COBS framing, CRC16, FreeRTOS tasks, dispatch.
#include "protocol.h"
#include "modules.h"
#include "link.h"
#include <esp_log.h>
#include <atomic>  // inter-task counters/flags: atomic is the correct primitive (volatile is not)

// ---- CRC-16/CCITT-FALSE (table-driven) --------------------------------------
// A 256-entry lookup table (512 B in rodata) replaces the naive per-byte
// 8-iteration bit loop, cutting the op count to roughly 1/8 on the hot path
// (every tx frame and every received frame). The table uses the standard
// parameters: poly=0x1021, init=0xFFFF, no input/output reflection — results
// are bit-identical to Python's binascii.crc_hqx(data, 0xFFFF).
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

uint16_t crc16_ccitt(const uint8_t* data, uint16_t len) {
  uint16_t crc = 0xFFFF;
  for (uint16_t i = 0; i < len; i++)
    crc = (uint16_t)(crc << 8) ^ crc16_table[(uint8_t)((crc >> 8) ^ data[i])];
  return crc;
}

// ---- COBS (Consistent Overhead Byte Stuffing) ---------------------------------
// cobs_encode: encodes in-place to out; returns the encoded byte count.
// out must be sized at least len + len/254 + 1 (worst-case overhead).
static uint16_t cobs_encode(const uint8_t* in, uint16_t len, uint8_t* out) {
  uint16_t ri = 0, wi = 1, code_i = 0;
  uint8_t code = 1;
  while (ri < len) {
    if (in[ri] == 0) {
      out[code_i] = code;
      code = 1; code_i = wi++;
      ri++;
    } else {
      out[wi++] = in[ri++];
      if (++code == 0xFF) { out[code_i] = code; code = 1; code_i = wi++; }
    }
  }
  out[code_i] = code;
  return wi;
}

// cobs_decode: decodes in to out; returns decoded byte count, or 0 on malformed input.
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

// ---- outbound queue + tx task ---------------------------------------------------
// dest controls where each frame is sent:
//   DEST_ALL  — serial always; BLE as well when the client is authenticated.
//   LINK_BLE  — BLE only; used for the pre-authentication SYS_AUTH exchange.
#define DEST_ALL 0xFF

struct Frame { uint8_t flags; uint8_t seq; uint16_t cmd; uint16_t len; uint8_t dest; uint8_t* buf; };

static QueueHandle_t txq;
static std::atomic<bool> tx_busy{false};       // tx_task busy-with-a-frame flag (read by proto_tx_flush)
static std::atomic<uint32_t> dropped{0};       // frames dropped across all producer tasks

// Reply routing: a reply goes back only on the link its request came from.
// Broadcasting to serial during a BLE session blocks tx_task ~180 ms on a 2 KB
// reply into the idle 115200-baud UART — the old BLE throughput ceiling.
// Handlers don't carry the origin, but a reply is always enqueued from the task
// that dispatched it (rx_task inline, slow_task queued), so a per-task origin set
// at dispatch time suffices.
static TaskHandle_t rx_task_h, slow_task_h;
static uint8_t origin_rx = DEST_ALL, origin_slow = DEST_ALL;

static uint8_t reply_dest() {
  TaskHandle_t t = xTaskGetCurrentTaskHandle();
  if (t == rx_task_h) return origin_rx;
  if (t == slow_task_h) return origin_slow;
  return DEST_ALL;  // unknown task (shouldn't happen): fall back to broadcast
}

static void enqueue_frame(uint8_t flags, uint8_t seq, uint16_t cmd,
                          const uint8_t* data, uint16_t len, uint8_t dest = DEST_ALL) {
  if (!txq) { dropped++; return; }  // proto_log_hook_install() may call this before proto_init()
  if (len > MAX_PAYLOAD) { dropped++; return; }
  Frame f = { flags, seq, cmd, len, dest, nullptr };
  if (len) {
    f.buf = (uint8_t*)malloc(len);
    if (!f.buf) { dropped++; return; }
    memcpy(f.buf, data, len);
  }
  // Replies (seq != 0): the host is blocking, waiting for the response, so
  // allow a short wait if the queue is temporarily full.
  // Events (seq == 0): best-effort — drop immediately if the queue is full.
  TickType_t wait = seq != 0 ? pdMS_TO_TICKS(250) : 0;
  if (xQueueSend(txq, &f, wait) != pdTRUE) {
    free(f.buf);
    dropped++;
  }
}

static void tx_task(void*) {
  static uint8_t logical[MAX_FRAME];
  static uint8_t encoded[ENC_BUF_SIZE];
  Frame f;
  for (;;) {
    if (xQueueReceive(txq, &f, portMAX_DELAY) != pdTRUE) continue;
    tx_busy = true;
    logical[0] = f.flags;
    logical[1] = f.seq;
    wr16(logical + 2, f.cmd);
    if (f.len) memcpy(logical + 4, f.buf, f.len);
    free(f.buf);
    wr16(logical + 4 + f.len, crc16_ccitt(logical, 4 + f.len));
    uint16_t n = cobs_encode(logical, 4 + f.len + 2, encoded);
    encoded[n++] = 0x00;
    if (f.dest == LINK_USB) {
      Serial.write(encoded, n);
    } else if (f.dest == LINK_BLE) {
      link_ble_write(encoded, n);
    } else {  // DEST_ALL: events / boot banner
      // With a BLE client attached, copy to serial only if it fits the TX ring
      // now — blocking into the idle 115200-baud UART would stall the BLE frames
      // behind it. USB-only keeps the blocking write (there the UART is the
      // requester's link, so back-pressure is correct pacing).
      if (!link_ble_authed() || Serial.availableForWrite() >= (int)n)
        Serial.write(encoded, n);
      if (link_ble_authed()) link_ble_write(encoded, n);
    }
    tx_busy = false;
  }
}

void proto_reply(uint8_t seq, uint16_t cmd, const uint8_t* data, uint16_t len) {
  if (seq == 0) return;  // fire-and-forget: no reply
  enqueue_frame(0, seq, cmd, data, len, reply_dest());
}

void proto_reply_ok(uint8_t seq, uint16_t cmd) { proto_reply(seq, cmd, nullptr, 0); }

void proto_reply_err(uint8_t seq, uint16_t cmd, uint8_t status) {
  if (seq == 0) return;
  enqueue_frame(FLAG_ERROR, seq, cmd, &status, 1, reply_dest());
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
           (unsigned long)ESP.getFreeHeap());
  proto_log(1, msg);
}

// ---- IDF log capture ---------------------------------------------------------
// The Wi-Fi/BT stacks emit log output via esp_log, which normally goes to
// UART0 — the same port carrying the COBS frame stream. Those raw bytes would
// land in the middle of frames and corrupt them. This hook redirects all IDF
// log output into SYS_LOG events instead.
// Note: ROM boot output and crash/panic text still go to UART0 as raw bytes;
// they cannot be intercepted here.
static int bridge_vprintf(const char* fmt, va_list ap) {
#if BRIDGE_NATIVE_USB
  return vprintf(fmt, ap);  // UART0 is free on native-USB chips: keep IDF logs
#else
  char line[160];
  int n = vsnprintf(line, sizeof(line), fmt, ap);
  // esp_log appends CR/LF; strip it since SYS_LOG delivers one line at a time.
  size_t L = n < 0 ? 0 : (n < (int)sizeof(line) ? (size_t)n : sizeof(line) - 1);
  while (L && (line[L - 1] == '\n' || line[L - 1] == '\r')) line[--L] = 0;
  if (L) proto_log(1, line);  // no-op before proto_init() (txq guard)
  return n;
#endif
}

void proto_log_hook_install() {
  esp_log_set_vprintf(bridge_vprintf);
}

void proto_tx_flush() {
  while (uxQueueMessagesWaiting(txq) > 0 || tx_busy) vTaskDelay(1);
  Serial.flush();
}

uint32_t proto_dropped_events() { return dropped; }

// ---- blocking-handler queue (bridge_slow) -----------------------------------------
// Handlers for WIFI, NET, BLE, and related modules can block for seconds
// (e.g. TCP connect, BLE connect) and share state with socket polling.
// To avoid blocking rx_task, all such handlers run exclusively on slow_task —
// which lives on CORE_RADIO, next to the stacks most of them call into.
struct Req { uint16_t cmd; uint8_t seq; uint8_t origin; uint16_t len; uint8_t* buf; };
static QueueHandle_t slowq;

static void slow_dispatch(uint16_t cmd, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint8_t mod = cmd >> 8, op = cmd & 0xFF;
  switch (mod) {
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
      origin_slow = r.origin;  // reply_dest() routes this request's replies
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

// ---- RX & dispatch (rx_task) -------------------------------------------------------
// Each link has its own COBS accumulator because bytes from USB and BLE
// arrive independently and may interleave.
struct RxState {
  uint8_t acc[ENC_BUF_SIZE];
  uint16_t len = 0;
  bool overflow = false;
};
static RxState rxstate[2];  // [LINK_USB], [LINK_BLE]
static uint8_t rxframe[MAX_FRAME];

static void dispatch(uint8_t origin, uint8_t seq, uint16_t cmd,
                     const uint8_t* p, uint16_t len) {
  origin_rx = origin;  // reply_dest() routes inline handlers' replies
  uint8_t mod = cmd >> 8, op = cmd & 0xFF;
  switch (mod) {
    // Fast handlers: run inline on rx_task.
    case MOD_SYS:   sys_handle(op, seq, p, len); break;
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
    // 1-Wire is inline by necessity, not speed: its bit-banged slots mask
    // interrupts for up to 70 µs, which must never happen on the radio core
    // where slow_task lives (the BT controller has hard real-time deadlines).
    // Worst op is a ROM search (~tens of ms) — same class as an I2C scan.
    case MOD_ONEWIRE: onewire_handle(op, seq, p, len); break;
    // Everything else is a slow / stateful handler (WIFI/NET/ESPNOW/BLE/RMT/
    // FS/OTA/TWAI/I2S/ETH/CAM) — hand off to slow_task, which replies
    // ST_UNKNOWN_CMD for any module it doesn't recognise.
    default:        slow_enqueue(cmd, seq, origin, p, len); break;
  }
}

// Constant-time-ish password compare (no early exit on mismatch).
static bool password_ok(const uint8_t* p, uint16_t len) {
  const char* pw = link_ble_password();
  uint16_t pwlen = strlen(pw);
  uint8_t diff = (len == pwlen) ? 0 : 1;
  for (uint16_t i = 0; i < len; i++) diff |= p[i] ^ (uint8_t)pw[i % (pwlen ? pwlen : 1)];
  return diff == 0;  // empty BRIDGE_PASSWORD = open access
}

// SYS_AUTH + the BLE auth gate run here, before normal dispatch, because they
// are link-layer concerns: replies must reach a not-yet-authenticated client.
static bool handle_auth(uint8_t origin, uint8_t seq, uint16_t cmd,
                        const uint8_t* p, uint16_t len) {
  uint8_t dest = origin == LINK_BLE ? LINK_BLE : DEST_ALL;
  if (cmd == SYS_AUTH) {
    if (origin != LINK_BLE) {  // USB implies physical access: always granted
      enqueue_frame(0, seq, cmd, nullptr, 0, dest);
      return true;
    }
    if (password_ok(p, len)) {
      link_ble_set_authed(true);
      enqueue_frame(0, seq, cmd, nullptr, 0, LINK_BLE);
      // Boot-banner equivalent so the host handshake works like USB.
      uint8_t info[64];
      enqueue_frame(FLAG_EVENT, 0, SYS_READY, info, sys_build_info(info), LINK_BLE);
      proto_log_heap("ble: authed");  // heap visibility on every session
    } else {
      uint8_t st = ST_DENIED;
      enqueue_frame(FLAG_ERROR, seq, cmd, &st, 1, LINK_BLE);
    }
    return true;
  }
  if (origin == LINK_BLE && !link_ble_authed()) {
    uint8_t st = ST_DENIED;
    enqueue_frame(FLAG_ERROR, seq, cmd, &st, 1, LINK_BLE);
    return true;
  }
  return false;
}

// Corrupt frames received on the USB serial link (bad COBS, short, CRC fail).
// One lost or flipped byte lands here — exposed via SYS_FREE_HEAP so a host
// retry warning can be traced to actual line corruption instead of guesswork.
static uint32_t serial_rx_errors = 0;
uint32_t link_serial_rx_errors() { return serial_rx_errors; }

static void handle_encoded(uint8_t origin, const uint8_t* enc, uint16_t enclen) {
  uint16_t n = cobs_decode(enc, enclen, rxframe);
  if (n < 6 || crc16_ccitt(rxframe, n - 2) != rd16(rxframe + n - 2)) {
    // Corrupted frame: drop it and let the host retry on timeout.
    if (origin == LINK_USB) serial_rx_errors++;
    return;
  }
  uint8_t seq = rxframe[1];
  uint16_t cmd = rd16(rxframe + 2);
  if (handle_auth(origin, seq, cmd, rxframe + 4, n - 6)) return;
  dispatch(origin, seq, cmd, rxframe + 4, n - 6);
}

static void pump_bytes(uint8_t origin, const uint8_t* chunk, int n) {
  RxState& rx = rxstate[origin];
  for (int i = 0; i < n; i++) {
    uint8_t b = chunk[i];
    if (b == 0x00) {
      if (!rx.overflow && rx.len > 0) handle_encoded(origin, rx.acc, rx.len);
      rx.len = 0;
      rx.overflow = false;
    } else {
      if (rx.len < sizeof(rx.acc)) {
        rx.acc[rx.len++] = b;
      } else {  // accumulator full (e.g. a lost delimiter merged two frames); discard until the next 0x00
        if (!rx.overflow && origin == LINK_USB) serial_rx_errors++;
        rx.overflow = true;
      }
    }
  }
}

// Returns true if any bytes were processed; rx_task yields briefly when false.
static bool proto_pump_rx() {
  static uint8_t chunk[256];
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
    pump_bytes(LINK_USB, chunk, n);
  }
  for (;;) {
    uint16_t n = link_ble_read(chunk, sizeof(chunk));
    if (n == 0) break;
    any = true;
    pump_bytes(LINK_BLE, chunk, n);
  }
  return any;
}

static void rx_task(void*) {
  uint16_t spins = 0;
  for (;;) {
    bool busy = proto_pump_rx();
    gpio_poll();   // ISR edge queue -> events
    uart_poll();   // secondary UART RX -> events
    // Yield regularly. Idle -> sleep a tick. Under sustained traffic we still
    // must yield every RX_YIELD_EVERY loops, or this priority-10 task starves
    // the core's idle task and trips the idle Task-WDT — a reset that looks
    // like a random stall under heavy load.
    if (!busy) {
      spins = 0;
      vTaskDelay(1);
    } else if (++spins >= RX_YIELD_EVERY) {
      spins = 0;
      vTaskDelay(1);
    }
  }
}

// ---- init / start -----------------------------------------------------------------
// proto_init: create the TX and net-task queues. Must be called before any
// proto_* function. proto_start: spawn the three bridge tasks; call at the
// end of setup() after all other init is done.
void proto_init() {
  txq = xQueueCreate(48, sizeof(Frame));
  slowq = xQueueCreate(SLOWQ_DEPTH, sizeof(Req));
}

void proto_start() {
  // Both cores work on dual-core chips — see the task-layout note in config.h:
  // TX and the blocking handlers ride the radio core, RX + fast handlers own
  // the app core, so reply N transmits while command N+1 executes.
  xTaskCreatePinnedToCore(tx_task, "bridge_tx", TX_TASK_STACK, nullptr,
                          TX_TASK_PRIO, nullptr, TX_TASK_CORE);
  xTaskCreatePinnedToCore(rx_task, "bridge_rx", RX_TASK_STACK, nullptr,
                          RX_TASK_PRIO, &rx_task_h, RX_TASK_CORE);
  xTaskCreatePinnedToCore(slow_task, "bridge_slow", SLOW_TASK_STACK, nullptr,
                          SLOW_TASK_PRIO, &slow_task_h, SLOW_TASK_CORE);
}
