// NVS (nRF52): persistent key/value store backed by LittleFS — each key is a
// file under /nvs/<key>. Values are raw bytes (host encodes/decodes types).
// Counterpart to src/esp/mod_nvs.cpp (which uses ESP Preferences). Runs on
// slow_task. Keys are limited to 15 bytes to match the ESP NVS contract.
#if defined(ARDUINO_ARCH_NRF52)
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include <InternalFileSystem.h>

using namespace Adafruit_LittleFS_Namespace;

bool nrf_internalfs_begin();  // defined in plat_nrf.cpp

#define NVS_KEY_MAX 15
#define NVS_DIR "/nvs"

// Build "/nvs/<key>" into out (>= 6 + NVS_KEY_MAX bytes). Returns false on a bad key.
static bool key_path(const uint8_t* k, uint8_t klen, char* out) {
  if (klen == 0 || klen > NVS_KEY_MAX) return false;
  memcpy(out, NVS_DIR "/", 5);
  memcpy(out + 5, k, klen);
  out[5 + klen] = 0;
  return true;
}

void nvs_handle_cmd(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_NVS, op);
  if (!nrf_internalfs_begin()) { proto_reply_err(seq, cmd, ST_IO); return; }
  char path[6 + NVS_KEY_MAX + 1];

  switch (op) {
    case 0x01: {  // SET: klen|key|data..
      if (len < 1 || !key_path(p + 1, p[0], path) || len < 1 + p[0])
        { proto_reply_err(seq, cmd, ST_BAD_ARGS); break; }
      InternalFS.mkdir(NVS_DIR);
      size_t vlen = len - 1 - p[0];
      File f = InternalFS.open(path, FILE_O_WRITE);
      if (!f) { proto_reply_err(seq, cmd, ST_IO); break; }
      f.truncate(0);
      f.seek(0);
      size_t w = vlen ? f.write(p + 1 + p[0], vlen) : 0;
      f.close();
      (w == vlen) ? proto_reply_ok(seq, cmd) : proto_reply_err(seq, cmd, ST_IO);
      break;
    }
    case 0x02: {  // GET: key -> data
      if (len == 0 || len > NVS_KEY_MAX) { proto_reply_err(seq, cmd, ST_BAD_ARGS); break; }
      key_path(p, (uint8_t)len, path);
      if (!InternalFS.exists(path)) { proto_reply_err(seq, cmd, ST_NOT_FOUND); break; }
      File f = InternalFS.open(path, FILE_O_READ);
      if (!f) { proto_reply_err(seq, cmd, ST_NOT_FOUND); break; }
      uint32_t n = f.size();
      if (n > MAX_PAYLOAD) n = MAX_PAYLOAD;
      uint8_t* buf = (uint8_t*)malloc(n ? n : 1);
      if (!buf) { f.close(); proto_reply_err(seq, cmd, ST_NO_MEM); break; }
      int got = f.read(buf, n);
      f.close();
      proto_reply(seq, cmd, buf, got > 0 ? (uint16_t)got : 0);
      free(buf);
      break;
    }
    case 0x03:  // DEL: key
      if (len == 0 || len > NVS_KEY_MAX) { proto_reply_err(seq, cmd, ST_BAD_ARGS); break; }
      key_path(p, (uint8_t)len, path);
      InternalFS.remove(path) ? proto_reply_ok(seq, cmd)
                              : proto_reply_err(seq, cmd, ST_NOT_FOUND);
      break;
    case 0x04: {  // KEYS -> n u8|{klen u8|key}*n
      uint8_t buf[512];
      uint16_t w = 1;
      uint8_t n = 0;
      File dir = InternalFS.open(NVS_DIR, FILE_O_READ);
      if (dir && dir.isDirectory()) {
        for (File e = dir.openNextFile(); e; e = dir.openNextFile()) {
          const char* nm = e.name();
          uint8_t kl = nm ? strlen(nm) : 0;
          if (kl > NVS_KEY_MAX) kl = NVS_KEY_MAX;
          if (kl && w + 1 + kl <= sizeof(buf)) {
            buf[w++] = kl;
            memcpy(buf + w, nm, kl);
            w += kl;
            n++;
          }
          e.close();
        }
      }
      if (dir) dir.close();
      buf[0] = n;
      proto_reply(seq, cmd, buf, w);
      break;
    }
    case 0x05:  // CLEAR: remove the whole namespace dir (recreated lazily on next SET)
      InternalFS.rmdir_r(NVS_DIR);
      proto_reply_ok(seq, cmd);
      break;
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#endif  // ARDUINO_ARCH_NRF52
