// NVS: persistent key/value bytes in the "user" namespace (kept apart from
// the bridge's own "bridge" namespace). Typed encoding is the host's job.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

#if BRIDGE_HAS_NVS

#include <Preferences.h>
#include <nvs.h>

#define NVS_NS "user"
#define NVS_KEY_MAX 15  // NVS limit

// Copies a length-prefixed or whole-payload key into a NUL-terminated buffer.
static bool take_key(const uint8_t* p, uint16_t len, char* out) {
  if (len == 0 || len > NVS_KEY_MAX) return false;
  memcpy(out, p, len);
  out[len] = 0;
  return true;
}

void nvs_handle_cmd(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_NVS, op);
  char key[NVS_KEY_MAX + 1];
  Preferences prefs;
  if (!prefs.begin(NVS_NS, false)) { proto_reply_err(seq, cmd, ST_IO); return; }
  switch (op) {
    case 0x01: {  // SET: klen|key|data..
      if (len < 1 || !take_key(p + 1, p[0], key) || len < 1 + p[0])
        { proto_reply_err(seq, cmd, ST_BAD_ARGS); break; }
      size_t vlen = len - 1 - p[0];
      if (prefs.putBytes(key, p + 1 + p[0], vlen) == vlen) proto_reply_ok(seq, cmd);
      else proto_reply_err(seq, cmd, ST_IO);
      break;
    }
    case 0x02: {  // GET: key -> data
      if (!take_key(p, len, key)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); break; }
      if (!prefs.isKey(key)) { proto_reply_err(seq, cmd, ST_NOT_FOUND); break; }
      size_t n = prefs.getBytesLength(key);
      uint8_t* buf = (uint8_t*)malloc(n ? n : 1);
      if (!buf) { proto_reply_err(seq, cmd, ST_NO_MEM); break; }
      prefs.getBytes(key, buf, n);
      proto_reply(seq, cmd, buf, n);
      free(buf);
      break;
    }
    case 0x03:  // DEL: key
      if (!take_key(p, len, key)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); break; }
      prefs.remove(key) ? proto_reply_ok(seq, cmd)
                        : proto_reply_err(seq, cmd, ST_NOT_FOUND);
      break;
    case 0x04: {  // KEYS -> n u8|{klen u8|key}*n
      uint8_t buf[512];
      uint16_t w = 1;
      uint8_t n = 0;
      nvs_iterator_t it = nullptr;
      esp_err_t err = nvs_entry_find("nvs", NVS_NS, NVS_TYPE_ANY, &it);
      while (err == ESP_OK && n < 255) {
        nvs_entry_info_t info;
        nvs_entry_info(it, &info);
        uint8_t kl = strlen(info.key);
        if (w + 1 + kl > sizeof(buf)) break;
        buf[w++] = kl;
        memcpy(buf + w, info.key, kl);
        w += kl;
        n++;
        err = nvs_entry_next(&it);
      }
      nvs_release_iterator(it);
      buf[0] = n;
      proto_reply(seq, cmd, buf, w);
      break;
    }
    case 0x05:  // CLEAR
      prefs.clear() ? proto_reply_ok(seq, cmd) : proto_reply_err(seq, cmd, ST_IO);
      break;
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
  prefs.end();
}

#else

void nvs_handle_cmd(uint8_t, uint8_t seq, const uint8_t*, uint16_t) {
  proto_reply_err(seq, CMD(MOD_NVS, 0), ST_UNSUPPORTED);
}

#endif
