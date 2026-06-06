// Camera (opt-in BRIDGE_ENABLE_CAM; esp32/s2/s3 + PSRAM). Bridge over esp32-camera.
// Host sends full pin map (presets in espbridge.camera); capture held, read in <=2 KB chunks. JPEG snapshots, not video. net_task.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

#if BRIDGE_CAM

#include <esp_camera.h>

static camera_fb_t* held = nullptr;
static bool cam_up = false;

void cam_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_CAM, op);
  switch (op) {
    case 0x01: {  // INIT: 16x pin i8 | xclk_mhz | framesize | jpeg_q | fb_count
      if (len < 20) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (cam_up) { proto_reply_err(seq, cmd, ST_BUSY); return; }
      if (!psramFound()) { proto_reply_err(seq, cmd, ST_UNSUPPORTED); return; }
      const int8_t* q = (const int8_t*)p;
      camera_config_t cfg = {};
      cfg.pin_pwdn = q[0];
      cfg.pin_reset = q[1];
      cfg.pin_xclk = q[2];
      cfg.pin_sccb_sda = q[3];
      cfg.pin_sccb_scl = q[4];
      cfg.pin_d7 = q[5];  cfg.pin_d6 = q[6];  cfg.pin_d5 = q[7];  cfg.pin_d4 = q[8];
      cfg.pin_d3 = q[9];  cfg.pin_d2 = q[10]; cfg.pin_d1 = q[11]; cfg.pin_d0 = q[12];
      cfg.pin_vsync = q[13];
      cfg.pin_href = q[14];
      cfg.pin_pclk = q[15];
      cfg.xclk_freq_hz = (uint32_t)p[16] * 1000000UL;
      cfg.ledc_timer = LEDC_TIMER_3;      // clear of mod_pwm channels
      cfg.ledc_channel = LEDC_CHANNEL_7;
      cfg.pixel_format = PIXFORMAT_JPEG;
      cfg.frame_size = (framesize_t)p[17];
      cfg.jpeg_quality = p[18];
      cfg.fb_count = p[19] ? p[19] : 1;
      cfg.fb_location = CAMERA_FB_IN_PSRAM;
      cfg.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
      if (esp_camera_init(&cfg) != ESP_OK) { proto_reply_err(seq, cmd, ST_IO); return; }
      cam_up = true;
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x02: {  // CAPTURE -> len u32|w u16|h u16|format u8 (frame held)
      if (!cam_up) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      if (held) { esp_camera_fb_return(held); held = nullptr; }
      held = esp_camera_fb_get();
      if (!held) { proto_reply_err(seq, cmd, ST_IO); return; }
      uint8_t buf[9];
      wr32(buf, held->len);
      wr16(buf + 4, held->width);
      wr16(buf + 6, held->height);
      buf[8] = (uint8_t)held->format;
      proto_reply(seq, cmd, buf, 9);
      break;
    }
    case 0x03: {  // READ: offset u32|n u16 -> data
      if (len < 6) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (!held) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      uint32_t off = rd32(p);
      uint16_t n = rd16(p + 4);
      if (n > MAX_PAYLOAD - 8) n = MAX_PAYLOAD - 8;
      if (off >= held->len) { proto_reply(seq, cmd, nullptr, 0); return; }
      if (off + n > held->len) n = held->len - off;
      proto_reply(seq, cmd, held->buf + off, n);
      break;
    }
    case 0x04:  // RELEASE
      if (held) { esp_camera_fb_return(held); held = nullptr; }
      proto_reply_ok(seq, cmd);
      break;
    case 0x05: {  // SET: prop u8|val i32 -> sensor_t setter
      if (len < 5) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      sensor_t* s = cam_up ? esp_camera_sensor_get() : nullptr;
      if (!s) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      int v = (int32_t)rd32(p + 1);
      int r;
      switch (p[0]) {  // ids mirror espbridge.camera
        case 0:  r = s->set_framesize(s, (framesize_t)v); break;
        case 1:  r = s->set_quality(s, v); break;
        case 2:  r = s->set_brightness(s, v); break;
        case 3:  r = s->set_contrast(s, v); break;
        case 4:  r = s->set_saturation(s, v); break;
        case 5:  r = s->set_vflip(s, v); break;
        case 6:  r = s->set_hmirror(s, v); break;
        case 7:  r = s->set_special_effect(s, v); break;
        case 8:  r = s->set_whitebal(s, v); break;
        case 9:  r = s->set_exposure_ctrl(s, v); break;
        case 10: r = s->set_aec_value(s, v); break;
        case 11: r = s->set_gain_ctrl(s, v); break;
        case 12: r = s->set_agc_gain(s, v); break;
        default: proto_reply_err(seq, cmd, ST_BAD_ARGS); return;
      }
      r == 0 ? proto_reply_ok(seq, cmd) : proto_reply_err(seq, cmd, ST_IO);
      break;
    }
    case 0x06:  // DEINIT
      if (held) { esp_camera_fb_return(held); held = nullptr; }
      if (cam_up) { esp_camera_deinit(); cam_up = false; }
      proto_reply_ok(seq, cmd);
      break;
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#else

UNSUPPORTED_STUB(cam_handle, MOD_CAM)

#endif
