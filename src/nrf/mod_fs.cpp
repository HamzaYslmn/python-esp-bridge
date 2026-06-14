// FS (nRF52): LittleFS on internal flash via InternalFS. Counterpart to
// src/esp/mod_fs.cpp, but only filesystem id 0 (LittleFS) — no SD. Runs on
// slow_task. The Adafruit LittleFS wrapper allows only ONE open file at a time,
// so a second OPEN before CLOSE replies ST_BUSY. No mtime is tracked (STAT
// reports 0). MOUNT/DF report 0/0 — the wrapper exposes no usage query.
#if defined(ARDUINO_ARCH_NRF52)
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include <InternalFileSystem.h>

using namespace Adafruit_LittleFS_Namespace;

bool nrf_internalfs_begin();  // plat_nrf.cpp

#define FS_PATH_MAX 128

static File dfile(InternalFS);  // the single open data file
static bool dopen = false;

static bool parse_path(const uint8_t* p, uint16_t len, char* out) {
  if (len == 0 || len >= FS_PATH_MAX || p[0] != '/') return false;
  memcpy(out, p, len);
  out[len] = 0;
  return true;
}

void fs_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_FS, op);
  char path[FS_PATH_MAX];

  // Every op starts with a filesystem id; only LittleFS (0) exists here.
  // (READ/WRITE/SEEK/CLOSE start with an fd instead — handled in their cases.)
  switch (op) {
    case 0x01:    // MOUNT: fs|... -> total_kb|used_kb
    case 0x0D: {  // DF: fs -> total_kb|used_kb
      NEED(1);
      if (p[0] != 0) { proto_reply_err(seq, cmd, ST_UNSUPPORTED); return; }
      if (!nrf_internalfs_begin()) { proto_reply_err(seq, cmd, ST_IO); return; }
      uint8_t buf[8] = {0};  // usage not queryable on this wrapper -> 0/0
      proto_reply(seq, cmd, buf, 8);
      break;
    }

    case 0x02:  // UMOUNT: fs (InternalFS stays mounted; no-op)
      NEED(1);
      proto_reply_ok(seq, cmd);
      break;

    case 0x03: {  // OPEN: fs|mode|path -> fd|size u32
      NEED(2);
      if (p[0] != 0 || !parse_path(p + 2, len - 2, path)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (!nrf_internalfs_begin()) { proto_reply_err(seq, cmd, ST_IO); return; }
      if (dopen) { proto_reply_err(seq, cmd, ST_BUSY); return; }  // one file at a time
      uint8_t mode = p[1];
      dfile = InternalFS.open(path, mode == 0 ? FILE_O_READ : FILE_O_WRITE);
      if (!dfile) { proto_reply_err(seq, cmd, ST_NOT_FOUND); return; }
      if (mode == 1) { dfile.truncate(0); dfile.seek(0); }   // write: truncate
      else if (mode == 2) dfile.seek(dfile.size());          // append: seek to end
      dopen = true;
      uint8_t buf[5] = { 0 };  // fd is always 0 (single file)
      write_be32(buf + 1, dfile.size());
      proto_reply(seq, cmd, buf, 5);
      break;
    }

    case 0x04: {  // READ: fd|n u16 -> data (short = EOF)
      if (len < 3) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (p[0] != 0 || !dopen) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      uint16_t n = read_be16(p + 1);
      if (n > MAX_PAYLOAD - 8) n = MAX_PAYLOAD - 8;
      uint8_t* buf = (uint8_t*)malloc(n ? n : 1);
      if (!buf) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
      int got = dfile.read(buf, n);
      proto_reply(seq, cmd, buf, got > 0 ? (uint16_t)got : 0);
      free(buf);
      break;
    }

    case 0x05: {  // WRITE: fd|data -> written u16
      if (len < 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (p[0] != 0 || !dopen) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      size_t w = (len > 1) ? dfile.write(p + 1, len - 1) : 0;
      uint8_t buf[2];
      write_be16(buf, (uint16_t)w);
      proto_reply(seq, cmd, buf, 2);
      break;
    }

    case 0x06:  // SEEK: fd|pos u32
      if (len < 5) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (p[0] != 0 || !dopen) { proto_reply_err(seq, cmd, ST_NOT_INIT); return; }
      dfile.seek(read_be32(p + 1)) ? proto_reply_ok(seq, cmd)
                                   : proto_reply_err(seq, cmd, ST_BAD_ARGS);
      break;

    case 0x07:  // CLOSE: fd
      if (len < 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (dopen) { dfile.close(); dopen = false; }
      proto_reply_ok(seq, cmd);
      break;

    case 0x08: {  // LIST: fs|path; entries stream as FS_LIST_EVT, reply = count
      if (len < 1 || p[0] != 0 || !parse_path(p + 1, len - 1, path)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (!nrf_internalfs_begin()) { proto_reply_err(seq, cmd, ST_IO); return; }
      File dir = InternalFS.open(path, FILE_O_READ);
      if (!dir || !dir.isDirectory()) { if (dir) dir.close(); proto_reply_err(seq, cmd, ST_NOT_FOUND); return; }
      uint16_t count = 0;
      for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
        uint8_t evt[5 + 64];
        evt[0] = f.isDirectory();
        write_be32(evt + 1, f.size());
        const char* nm = f.name();
        uint8_t nl = nm ? strlen(nm) : 0;
        if (nl > 64) nl = 64;
        memcpy(evt + 5, nm, nl);
        proto_send_event(FS_LIST_EVT, evt, 5 + nl);
        f.close();
        count++;
      }
      dir.close();
      uint8_t buf[2];
      write_be16(buf, count);
      proto_reply(seq, cmd, buf, 2);
      break;
    }

    case 0x09: {  // STAT: fs|path -> size u32|isdir u8|mtime u32 (mtime always 0)
      if (len < 1 || p[0] != 0 || !parse_path(p + 1, len - 1, path)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (!nrf_internalfs_begin()) { proto_reply_err(seq, cmd, ST_IO); return; }
      if (!InternalFS.exists(path)) { proto_reply_err(seq, cmd, ST_NOT_FOUND); return; }
      File f = InternalFS.open(path, FILE_O_READ);
      if (!f) { proto_reply_err(seq, cmd, ST_NOT_FOUND); return; }
      uint8_t buf[9];
      write_be32(buf, f.size());
      buf[4] = f.isDirectory();
      write_be32(buf + 5, 0);  // no mtime on LittleFS
      f.close();
      proto_reply(seq, cmd, buf, 9);
      break;
    }

    case 0x0A:  // REMOVE: fs|path (file or empty dir)
      if (len < 1 || p[0] != 0 || !parse_path(p + 1, len - 1, path)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      (InternalFS.remove(path) || InternalFS.rmdir(path))
          ? proto_reply_ok(seq, cmd) : proto_reply_err(seq, cmd, ST_NOT_FOUND);
      break;

    case 0x0B: {  // RENAME: fs|from_len|from|to
      char to[FS_PATH_MAX];
      if (len < 2 || p[0] != 0 || len < (uint16_t)(2 + p[1]) ||
          !parse_path(p + 2, p[1], path) ||
          !parse_path(p + 2 + p[1], len - 2 - p[1], to)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      InternalFS.rename(path, to) ? proto_reply_ok(seq, cmd)
                                  : proto_reply_err(seq, cmd, ST_NOT_FOUND);
      break;
    }

    case 0x0C:  // MKDIR: fs|path
      if (len < 1 || p[0] != 0 || !parse_path(p + 1, len - 1, path)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      InternalFS.mkdir(path) ? proto_reply_ok(seq, cmd) : proto_reply_err(seq, cmd, ST_IO);
      break;

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#endif  // ARDUINO_ARCH_NRF52
