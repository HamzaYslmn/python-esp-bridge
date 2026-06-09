// Secondary UARTs (1, 2): TX from host, RX streamed back as events.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

static bool uart_inited[3];

static HardwareSerial* port_of(uint8_t idx) {
  if (idx == 1) return &Serial1;
#if SOC_UART_NUM > 2
  if (idx == 2) return &Serial2;
#endif
  return nullptr;  // UART0 is the bridge link itself
}

void uart_poll() {
  for (uint8_t idx = 1; idx <= 2; idx++) {
    if (!uart_inited[idx]) continue;
    HardwareSerial* u = port_of(idx);
    if (!u) continue;
    int avail = u->available();
    if (avail <= 0) continue;
    uint8_t buf[1 + UART_CHUNK];
    buf[0] = idx;
    int n = u->read(buf + 1, avail > UART_CHUNK ? UART_CHUNK : avail);
    if (n > 0) proto_send_event(UART_RX_EVT, buf, 1 + n);
  }
}

void uart_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_UART, op);
  if (len < 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
  uint8_t idx = p[0];
  HardwareSerial* u = port_of(idx);
  if (!u) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }

  switch (op) {
    case 0x01: {  // INIT: port, tx i8, rx i8, baud u32
      if (len < 7) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (uart_inited[idx]) u->end();
      u->setRxBufferSize(1024);
      u->begin(rd32(p + 3), SERIAL_8N1, (int8_t)p[2], (int8_t)p[1]);  // arduino-esp32 begin() order: baud, config, rx_pin, tx_pin
      uart_inited[idx] = true;
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x02: {  // WRITE: port, data..
      if (!uart_inited[idx]) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      if (len > 1) u->write(p + 1, len - 1);
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x03: {  // DEINIT
      if (uart_inited[idx]) { u->end(); uart_inited[idx] = false; }
      proto_reply_ok(seq, cmd);
      break;
    }

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}
