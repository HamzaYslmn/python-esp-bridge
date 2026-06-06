// python-esp-bridge — COBS framing, CRC16, FreeRTOS tasks, dispatch.
#include "protocol.h"
#include "modules.h"
#include "link.h"
#include <esp_log.h>

// ---- CRC-16/CCITT-FALSE -----------------------------------------------------
uint16_t crc16_ccitt(const uint8_t* data, uint16_t len) {
  uint16_t crc = 0xFFFF;
  for (uint16_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (uint8_t b = 0; b < 8; b++)
      crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
  }
  return crc;
}

// ---- COBS ---------------------------------------------------------------------
// Returns encoded length; out must hold len + len/254 + 1.
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

// Returns decoded length, or 0 on malformed input.
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
// dest: DEST_ALL broadcasts (serial always, BLE when authenticated);
// LINK_BLE targets only the BLE client (pre-auth SYS_AUTH conversation).
#define DEST_ALL 0xFF

struct Frame { uint8_t flags; uint8_t seq; uint16_t cmd; uint16_t len; uint8_t dest; uint8_t* buf; };

static QueueHandle_t txq;
static volatile bool tx_busy = false;
static volatile uint32_t dropped = 0;

static void enqueue_frame(uint8_t flags, uint8_t seq, uint16_t cmd,
                          const uint8_t* data, uint16_t len, uint8_t dest = DEST_ALL) {
  if (!txq) { dropped++; return; }  // log hook can fire before proto_init()
  if (len > MAX_PAYLOAD) { dropped++; return; }
  Frame f = { flags, seq, cmd, len, dest, nullptr };
  if (len) {
    f.buf = (uint8_t*)malloc(len);
    if (!f.buf) { dropped++; return; }
    memcpy(f.buf, data, len);
  }
  // Replies (seq != 0) briefly block when the queue is full — the host is
  // waiting for them. Events are best-effort: drop and count.
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
    if (f.dest == DEST_ALL) {
      Serial.write(encoded, n);
      if (link_ble_authed()) link_ble_write(encoded, n);
    } else if (f.dest == LINK_BLE) {
      link_ble_write(encoded, n);  // pre-auth: SYS_AUTH replies / ST_DENIED
    }
    tx_busy = false;
  }
}

void proto_reply(uint8_t seq, uint16_t cmd, const uint8_t* data, uint16_t len) {
  if (seq == 0) return;  // fire-and-forget: no reply
  enqueue_frame(0, seq, cmd, data, len);
}

void proto_reply_ok(uint8_t seq, uint16_t cmd) { proto_reply(seq, cmd, nullptr, 0); }

void proto_reply_err(uint8_t seq, uint16_t cmd, uint8_t status) {
  if (seq == 0) return;
  enqueue_frame(FLAG_ERROR, seq, cmd, &status, 1);
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
// The Wi-Fi/BT stacks log through esp_log; on UART links those bytes would land
// in the middle of COBS frames and corrupt them. Redirect everything into
// SYS_LOG events instead (ROM boot output and panics still hit UART0 raw).
static int bridge_vprintf(const char* fmt, va_list ap) {
#if BRIDGE_NATIVE_USB
  return vprintf(fmt, ap);  // UART0 is free on native-USB chips: keep IDF logs
#else
  char line[160];
  int n = vsnprintf(line, sizeof(line), fmt, ap);
  // Strip the trailing CR/LF esp_log appends; SYS_LOG is line-oriented.
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

// ---- net-task request queue ------------------------------------------------------
// WIFI/NET/BLE handlers may block for seconds (TCP connect, BLE connect) and
// share state with socket polling — they all run on net_task only.
struct Req { uint16_t cmd; uint8_t seq; uint16_t len; uint8_t* buf; };
static QueueHandle_t netq;

static void net_dispatch(uint16_t cmd, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint8_t mod = cmd >> 8, op = cmd & 0xFF;
  switch (mod) {
    case MOD_WIFI:   wifi_handle(op, seq, p, len); break;
    case MOD_NET:    net_handle(op, seq, p, len); break;
    case MOD_ESPNOW: espnow_handle(op, seq, p, len); break;
    case MOD_BLE:    ble_handle(op, seq, p, len); break;
    case MOD_RMT:    rmt_handle(op, seq, p, len); break;
    case MOD_ONEWIRE: onewire_handle(op, seq, p, len); break;
    case MOD_FS:     fs_handle(op, seq, p, len); break;
    case MOD_OTA:    ota_handle(op, seq, p, len); break;
    case MOD_TWAI:   twai_handle(op, seq, p, len); break;
    case MOD_I2S:    i2s_handle(op, seq, p, len); break;
    case MOD_ETH:    eth_handle(op, seq, p, len); break;
    case MOD_CAM:    cam_handle(op, seq, p, len); break;
    default:         proto_reply_err(seq, cmd, ST_UNKNOWN_CMD); break;
  }
}

static void net_task(void*) {
  Req r;
  for (;;) {
    // Wake at least every 2 ms to poll sockets / scan completion.
    if (xQueueReceive(netq, &r, pdMS_TO_TICKS(2)) == pdTRUE) {
      net_dispatch(r.cmd, r.seq, r.buf, r.len);
      free(r.buf);
    }
    wifi_poll();
    net_poll();
    twai_poll();
  }
}

static void net_enqueue(uint16_t cmd, uint8_t seq, const uint8_t* p, uint16_t len) {
  Req r = { cmd, seq, len, nullptr };
  if (len) {
    r.buf = (uint8_t*)malloc(len);
    if (!r.buf) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
    memcpy(r.buf, p, len);
  }
  if (xQueueSend(netq, &r, 0) != pdTRUE) {
    free(r.buf);
    proto_reply_err(seq, cmd, ST_BUSY);
  }
}

// ---- RX & dispatch (rx_task) -------------------------------------------------------
// One COBS accumulator per link: USB and BLE bytes may interleave arbitrarily.
struct RxState {
  uint8_t acc[ENC_BUF_SIZE];
  uint16_t len = 0;
  bool overflow = false;
};
static RxState rxstate[2];  // [LINK_USB], [LINK_BLE]
static uint8_t rxframe[MAX_FRAME];

static void dispatch(uint8_t seq, uint16_t cmd, const uint8_t* p, uint16_t len) {
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
    // Everything else is a slow / stateful handler (WIFI/NET/ESPNOW/BLE/RMT/
    // ONEWIRE/FS/OTA/TWAI/I2S/ETH/CAM) — hand off to net_task, which replies
    // ST_UNKNOWN_CMD for any module it doesn't recognise.
    default:        net_enqueue(cmd, seq, p, len); break;
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

static void handle_encoded(uint8_t origin, const uint8_t* enc, uint16_t enclen) {
  uint16_t n = cobs_decode(enc, enclen, rxframe);
  if (n < 6) return;  // hdr(4) + crc(2) minimum; silently drop garbage
  uint16_t crc = rd16(rxframe + n - 2);
  if (crc16_ccitt(rxframe, n - 2) != crc) return;  // corrupted: drop, host retries on timeout
  uint8_t seq = rxframe[1];
  uint16_t cmd = rd16(rxframe + 2);
  if (handle_auth(origin, seq, cmd, rxframe + 4, n - 6)) return;
  dispatch(seq, cmd, rxframe + 4, n - 6);
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
      if (rx.len < sizeof(rx.acc)) rx.acc[rx.len++] = b;
      else rx.overflow = true;  // discard until next delimiter
    }
  }
}

// Returns true if any bytes were processed (rx_task idles briefly otherwise).
static bool proto_pump_rx() {
  static uint8_t chunk[256];
  bool any = false;
  for (;;) {
    // Drain the UART ring buffer in blocks, not byte-by-byte HAL calls.
    // Asking read() only for what available() reports keeps the call
    // non-blocking on both HardwareSerial and HWCDC.
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
  for (;;) {
    bool busy = proto_pump_rx();
    gpio_poll();   // ISR edge queue -> events
    uart_poll();   // secondary UART RX -> events
    if (!busy) vTaskDelay(1);
  }
}

// ---- init / start -----------------------------------------------------------------
void proto_init() {
  txq = xQueueCreate(48, sizeof(Frame));
  netq = xQueueCreate(16, sizeof(Req));
}

void proto_start() {
  // Radio stacks (Wi-Fi/BT) live on core 0; keep the bridge on the app core.
  xTaskCreatePinnedToCore(tx_task, "bridge_tx", 4096, nullptr, 12, nullptr, BRIDGE_CORE);
  xTaskCreatePinnedToCore(rx_task, "bridge_rx", 8192, nullptr, 10, nullptr, BRIDGE_CORE);
  xTaskCreatePinnedToCore(net_task, "bridge_net", 8192, nullptr, 9, nullptr, BRIDGE_CORE);
}
