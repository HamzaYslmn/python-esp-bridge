// Secondary UART (nRF52): port 1 = Serial1 (Serial is the USB-CDC bridge link).
// TX from host, RX streamed back as UART_RX_EVT. Counterpart to src/esp/mod_uart.cpp.
// nRF sets pins via setPins(rx, tx) before begin(baud).
#if defined(ARDUINO_ARCH_NRF52)
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

static bool uart1_inited = false;

void uart_poll() {
  if (!uart1_inited || Serial1.available() <= 0) return;
  uint8_t buf[1 + UART_CHUNK];
  buf[0] = 1;  // port id
  int n = 0;   // nRF Uart has only single-byte read()
  while (n < UART_CHUNK && Serial1.available() > 0) buf[1 + n++] = (uint8_t)Serial1.read();
  if (n > 0) proto_send_event(UART_RX_EVT, buf, 1 + n);
}

void uart_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_UART, op);
  NEED(1);
  if (p[0] != 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }  // only Serial1 on this build

  switch (op) {
    case 0x01: {  // INIT: port, tx i8, rx i8, baud u32
      NEED(7);
      if (uart1_inited) Serial1.end();
      int8_t tx = (int8_t)p[1], rx = (int8_t)p[2];
      if (tx >= 0 && rx >= 0) Serial1.setPins((uint8_t)rx, (uint8_t)tx);  // else: variant default pins
      Serial1.begin(read_be32(p + 3));
      uart1_inited = true;
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x02:  // WRITE: port, data..
      if (!uart1_inited) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      if (len > 1) Serial1.write(p + 1, len - 1);
      proto_reply_ok(seq, cmd);
      break;

    case 0x03:  // DEINIT
      if (uart1_inited) { Serial1.end(); uart1_inited = false; }
      proto_reply_ok(seq, cmd);
      break;

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#endif  // ARDUINO_ARCH_NRF52
