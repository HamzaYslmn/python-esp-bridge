// TWAI (CAN bus) through the IDF driver. Needs an external transceiver
// (SN65HVD230/TJA1050) between the pins and the bus. Frames received are
// pushed to the host as TWAI_RX_EVT from twai_poll() on net_task.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

#if BRIDGE_HAS_TWAI

#include <driver/twai.h>

static bool twai_up = false;

static const twai_timing_config_t TIMINGS[] = {
  TWAI_TIMING_CONFIG_25KBITS(),  TWAI_TIMING_CONFIG_50KBITS(),
  TWAI_TIMING_CONFIG_100KBITS(), TWAI_TIMING_CONFIG_125KBITS(),
  TWAI_TIMING_CONFIG_250KBITS(), TWAI_TIMING_CONFIG_500KBITS(),
  TWAI_TIMING_CONFIG_800KBITS(), TWAI_TIMING_CONFIG_1MBITS(),
};

void twai_poll() {
  if (!twai_up) return;
  twai_message_t msg;
  // Drain a few frames per pass; the rest wait in the driver's RX queue.
  for (int i = 0; i < 8 && twai_receive(&msg, 0) == ESP_OK; i++) {
    uint8_t evt[5 + TWAI_FRAME_MAX_DLC];
    evt[0] = (msg.extd ? 1 : 0) | (msg.rtr ? 2 : 0);
    wr32(evt + 1, msg.identifier);
    uint8_t n = msg.data_length_code > TWAI_FRAME_MAX_DLC
                    ? TWAI_FRAME_MAX_DLC : msg.data_length_code;
    memcpy(evt + 5, msg.data, n);
    proto_send_event(TWAI_RX_EVT, evt, 5 + (msg.rtr ? 0 : n));
  }
}

void twai_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_TWAI, op);
  switch (op) {
    case 0x01: {  // INIT: tx|rx|mode|baud|[code u32|mask u32|single u8]
      if (len < 4 || p[2] > 2 || p[3] >= sizeof(TIMINGS) / sizeof(TIMINGS[0]))
        { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (twai_up) { proto_reply_err(seq, cmd, ST_BUSY); return; }
      twai_mode_t mode = p[2] == 1 ? TWAI_MODE_LISTEN_ONLY
                       : p[2] == 2 ? TWAI_MODE_NO_ACK : TWAI_MODE_NORMAL;
      twai_general_config_t g = TWAI_GENERAL_CONFIG_DEFAULT(
          (gpio_num_t)p[0], (gpio_num_t)p[1], mode);
      g.rx_queue_len = 16;
      twai_filter_config_t f = TWAI_FILTER_CONFIG_ACCEPT_ALL();
      if (len >= 13) {
        f.acceptance_code = rd32(p + 4);
        f.acceptance_mask = rd32(p + 8);
        f.single_filter = p[12] != 0;
      }
      if (twai_driver_install(&g, &TIMINGS[p[3]], &f) != ESP_OK
          || twai_start() != ESP_OK) {
        twai_driver_uninstall();
        proto_reply_err(seq, cmd, ST_IO);
        return;
      }
      twai_up = true;
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x02: {  // SEND: flags|id u32|data 0..8
      if (len < 5 || len > 13) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (!twai_up) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      twai_message_t msg = {};
      msg.extd = p[0] & 1;
      msg.rtr = (p[0] >> 1) & 1;
      msg.identifier = rd32(p + 1);
      msg.data_length_code = len - 5;
      memcpy(msg.data, p + 5, len - 5);
      esp_err_t err = twai_transmit(&msg, pdMS_TO_TICKS(50));
      err == ESP_OK ? proto_reply_ok(seq, cmd)
                    : proto_reply_err(seq, cmd, err == ESP_ERR_TIMEOUT ? ST_TIMEOUT : ST_IO);
      break;
    }
    case 0x03: {  // STATUS -> state u8|tx_err u8|rx_err u8|rx_missed u32
      twai_status_info_t st = {};
      if (!twai_up || twai_get_status_info(&st) != ESP_OK)
        { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      uint8_t buf[7];
      buf[0] = st.state == TWAI_STATE_RUNNING ? 1
             : st.state == TWAI_STATE_RECOVERING ? 2
             : st.state == TWAI_STATE_BUS_OFF ? 3 : 0;
      buf[1] = st.tx_error_counter > 255 ? 255 : st.tx_error_counter;
      buf[2] = st.rx_error_counter > 255 ? 255 : st.rx_error_counter;
      wr32(buf + 3, st.rx_missed_count);
      proto_reply(seq, cmd, buf, 7);
      break;
    }
    case 0x04:  // RECOVER (after bus-off)
      if (!twai_up) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      twai_initiate_recovery() == ESP_OK ? proto_reply_ok(seq, cmd)
                                         : proto_reply_err(seq, cmd, ST_IO);
      break;
    case 0x05:  // DEINIT
      if (twai_up) {
        twai_stop();
        twai_driver_uninstall();
        twai_up = false;
      }
      proto_reply_ok(seq, cmd);
      break;
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#else

void twai_poll() {}
void twai_handle(uint8_t, uint8_t seq, const uint8_t*, uint16_t) {
  proto_reply_err(seq, CMD(MOD_TWAI, 0), ST_UNSUPPORTED);
}

#endif
