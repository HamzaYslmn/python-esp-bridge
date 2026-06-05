// python-esp-bridge — COBS framing, CRC16, FreeRTOS tasks, dispatch.
#include "protocol.h"
#include "modules.h"

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
struct Frame { uint8_t flags; uint8_t seq; uint16_t cmd; uint16_t len; uint8_t* buf; };

static QueueHandle_t txq;
static volatile bool tx_busy = false;
static volatile uint32_t dropped = 0;

static void enqueue_frame(uint8_t flags, uint8_t seq, uint16_t cmd,
                          const uint8_t* data, uint16_t len) {
  if (len > MAX_PAYLOAD) { dropped++; return; }
  Frame f = { flags, seq, cmd, len, nullptr };
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
    Serial.write(encoded, n);
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
    case MOD_WIFI: wifi_handle(op, seq, p, len); break;
    case MOD_NET:  net_handle(op, seq, p, len); break;
    case MOD_BLE:  ble_handle(op, seq, p, len); break;
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
static uint8_t rxacc[ENC_BUF_SIZE];
static uint16_t rxlen = 0;
static bool rxoverflow = false;
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
    // Slow / stateful handlers: hand off to net_task.
    case MOD_WIFI:
    case MOD_NET:
    case MOD_BLE:   net_enqueue(cmd, seq, p, len); break;
    default:        proto_reply_err(seq, cmd, ST_UNKNOWN_CMD); break;
  }
}

static void handle_encoded(const uint8_t* enc, uint16_t enclen) {
  uint16_t n = cobs_decode(enc, enclen, rxframe);
  if (n < 6) return;  // hdr(4) + crc(2) minimum; silently drop garbage
  uint16_t crc = rd16(rxframe + n - 2);
  if (crc16_ccitt(rxframe, n - 2) != crc) return;  // corrupted: drop, host retries on timeout
  uint8_t seq = rxframe[1];
  uint16_t cmd = rd16(rxframe + 2);
  dispatch(seq, cmd, rxframe + 4, n - 6);
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
    for (int i = 0; i < n; i++) {
      uint8_t b = chunk[i];
      if (b == 0x00) {
        if (!rxoverflow && rxlen > 0) handle_encoded(rxacc, rxlen);
        rxlen = 0;
        rxoverflow = false;
      } else {
        if (rxlen < sizeof(rxacc)) rxacc[rxlen++] = b;
        else rxoverflow = true;  // discard until next delimiter
      }
    }
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
