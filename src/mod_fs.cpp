// FS: LittleFS + SD file/dir operations, data streamed in <=2 KB chunks. Runs on net_task (blocking I/O).
// Filesystem IDs: 0 = LittleFS, 1 = SD over SPI (uses its own bus BRIDGE_SPI_HOST1 — do NOT share with mod_spi host 1), 2 = SD_MMC.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

#if BRIDGE_HAS_FS

#include <FS.h>
#include <LittleFS.h>
#if BRIDGE_HAS_SD
#include <SD.h>
#include <SPI.h>
#endif
#if BRIDGE_HAS_SDMMC
#include <SD_MMC.h>
#endif

#define FS_MAX_FDS 8
#define FS_PATH_MAX 128

static File fds[FS_MAX_FDS];
#if BRIDGE_HAS_SD
static SPIClass* sd_bus = nullptr;
#endif

static fs::FS* fs_of(uint8_t id) {
  switch (id) {
    case 0: return &LittleFS;
#if BRIDGE_HAS_SD
    case 1: return &SD;
#endif
#if BRIDGE_HAS_SDMMC
    case 2: return &SD_MMC;
#endif
    default: return nullptr;
  }
}

static void df_of(uint8_t id, uint32_t* total_kb, uint32_t* used_kb) {
  uint64_t t = 0, u = 0;
  if (id == 0) { t = LittleFS.totalBytes(); u = LittleFS.usedBytes(); }
#if BRIDGE_HAS_SD
  else if (id == 1) { t = SD.totalBytes(); u = SD.usedBytes(); }
#endif
#if BRIDGE_HAS_SDMMC
  else if (id == 2) { t = SD_MMC.totalBytes(); u = SD_MMC.usedBytes(); }
#endif
  *total_kb = t / 1024;
  *used_kb = u / 1024;
}

// Copies a payload byte-range into out[] and NUL-terminates it. Returns false if the path is empty, too long, or doesn't start with '/'.
static bool take_path(const uint8_t* p, uint16_t len, char* out) {
  if (len == 0 || len >= FS_PATH_MAX || p[0] != '/') return false;
  memcpy(out, p, len);
  out[len] = 0;
  return true;
}

// Convenience for ops whose payload is [fs_id | ... | path]: resolves the filesystem from byte 0
// and parses the path starting at byte `off`. Returns nullptr if either step fails.
static fs::FS* take_fs_path(const uint8_t* p, uint16_t len, uint8_t off, char* out) {
  if (len < (uint16_t)off + 1) return nullptr;
  fs::FS* fs = fs_of(p[0]);
  return (fs && take_path(p + off, len - off, out)) ? fs : nullptr;
}

static void reply_df(uint8_t seq, uint16_t cmd, uint8_t id) {
  uint8_t buf[8];
  uint32_t total, used;
  df_of(id, &total, &used);
  wr32(buf, total);
  wr32(buf + 4, used);
  proto_reply(seq, cmd, buf, 8);
}

void fs_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_FS, op);
  char path[FS_PATH_MAX];
  switch (op) {
    case 0x01: {  // MOUNT: fs|[sd_spi: cs|sck|miso|mosi|freq_mhz] -> total|used kb
      NEED(1);
      bool ok = false;
      if (p[0] == 0) ok = LittleFS.begin(true /*format on first use*/);
#if BRIDGE_HAS_SD
      else if (p[0] == 1) {
        NEED(6);
        if (!sd_bus) sd_bus = new SPIClass(BRIDGE_SPI_HOST1);
        sd_bus->begin((int8_t)p[2], (int8_t)p[3], (int8_t)p[4], (int8_t)p[1]);
        ok = SD.begin(p[1], *sd_bus, p[5] * 1000000UL);
      }
#endif
#if BRIDGE_HAS_SDMMC
      else if (p[0] == 2) ok = SD_MMC.begin();
#endif
      else { proto_reply_err(seq, cmd, ST_UNSUPPORTED); return; }
      if (!ok) { proto_reply_err(seq, cmd, ST_IO); return; }
      reply_df(seq, cmd, p[0]);
      break;
    }

    case 0x02:  // UMOUNT: fs
      NEED(1);
      if (p[0] == 0) LittleFS.end();
#if BRIDGE_HAS_SD
      else if (p[0] == 1) SD.end();
#endif
#if BRIDGE_HAS_SDMMC
      else if (p[0] == 2) SD_MMC.end();
#endif
      proto_reply_ok(seq, cmd);
      break;

    case 0x03: {  // OPEN: fs|mode|path -> fd|size u32
      fs::FS* fs = take_fs_path(p, len, 2, path);
      if (!fs) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      int fd = -1;
      for (int i = 0; i < FS_MAX_FDS; i++) if (!fds[i]) { fd = i; break; }
      if (fd < 0) { proto_reply_err(seq, cmd, ST_BUSY); return; }
      const char* mode = p[1] == 1 ? FILE_WRITE : p[1] == 2 ? FILE_APPEND : FILE_READ;
      fds[fd] = fs->open(path, mode, p[1] == 1);
      if (!fds[fd]) { proto_reply_err(seq, cmd, ST_NOT_FOUND); return; }
      uint8_t buf[5] = { (uint8_t)fd };
      wr32(buf + 1, fds[fd].size());
      proto_reply(seq, cmd, buf, 5);
      break;
    }

    case 0x04: {  // READ: fd|n u16 -> data (short read = EOF)
      if (len < 3 || p[0] >= FS_MAX_FDS || !fds[p[0]])
        { proto_reply_err(seq, cmd, len < 3 ? ST_BAD_ARGS : ST_NOT_INIT); return; }
      uint16_t n = rd16(p + 1);
      if (n > MAX_PAYLOAD - 8) n = MAX_PAYLOAD - 8;
      uint8_t* buf = (uint8_t*)malloc(n ? n : 1);
      if (!buf) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
      size_t got = fds[p[0]].read(buf, n);
      proto_reply(seq, cmd, buf, got);
      free(buf);
      break;
    }

    case 0x05: {  // WRITE: fd|data -> written u16
      if (len < 1 || p[0] >= FS_MAX_FDS || !fds[p[0]])
        { proto_reply_err(seq, cmd, len < 1 ? ST_BAD_ARGS : ST_NOT_INIT); return; }
      size_t w = fds[p[0]].write(p + 1, len - 1);
      uint8_t buf[2];
      wr16(buf, w);
      proto_reply(seq, cmd, buf, 2);
      break;
    }

    case 0x06:  // SEEK: fd|pos u32
      if (len < 5 || p[0] >= FS_MAX_FDS || !fds[p[0]])
        { proto_reply_err(seq, cmd, len < 5 ? ST_BAD_ARGS : ST_NOT_INIT); return; }
      fds[p[0]].seek(rd32(p + 1)) ? proto_reply_ok(seq, cmd)
                                  : proto_reply_err(seq, cmd, ST_BAD_ARGS);
      break;

    case 0x07:  // CLOSE: fd
      if (len < 1 || p[0] >= FS_MAX_FDS) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      fds[p[0]].close();
      proto_reply_ok(seq, cmd);
      break;

    case 0x08: {  // LIST: fs|path; entries stream as FS_LIST_EVT, reply = count
      fs::FS* fs = take_fs_path(p, len, 1, path);
      if (!fs) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      File dir = fs->open(path);
      if (!dir || !dir.isDirectory()) { proto_reply_err(seq, cmd, ST_NOT_FOUND); return; }
      uint16_t count = 0;
      for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
        uint8_t evt[5 + 64];
        evt[0] = f.isDirectory();
        wr32(evt + 1, f.size());
        uint8_t nl = strlen(f.name());
        if (nl > 64) nl = 64;
        memcpy(evt + 5, f.name(), nl);
        proto_send_event(FS_LIST_EVT, evt, 5 + nl);
        count++;
      }
      uint8_t buf[2];
      wr16(buf, count);
      proto_reply(seq, cmd, buf, 2);
      break;
    }

    case 0x09: {  // STAT: fs|path -> size u32|isdir u8|mtime u32
      fs::FS* fs = take_fs_path(p, len, 1, path);
      if (!fs) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      File f = fs->open(path);
      if (!f) { proto_reply_err(seq, cmd, ST_NOT_FOUND); return; }
      uint8_t buf[9];
      wr32(buf, f.size());
      buf[4] = f.isDirectory();
      wr32(buf + 5, f.getLastWrite());
      proto_reply(seq, cmd, buf, 9);
      break;
    }

    case 0x0A: {  // REMOVE: fs|path (file or empty dir)
      fs::FS* fs = take_fs_path(p, len, 1, path);
      if (!fs) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      fs->remove(path) || fs->rmdir(path)
          ? proto_reply_ok(seq, cmd) : proto_reply_err(seq, cmd, ST_NOT_FOUND);
      break;
    }

    case 0x0B: {  // RENAME: fs|from_len|from|to
      fs::FS* fs = len >= 4 ? fs_of(p[0]) : nullptr;
      char to[FS_PATH_MAX];
      if (!fs || len < 2 + p[1] || !take_path(p + 2, p[1], path)
          || !take_path(p + 2 + p[1], len - 2 - p[1], to))
        { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      fs->rename(path, to) ? proto_reply_ok(seq, cmd)
                           : proto_reply_err(seq, cmd, ST_NOT_FOUND);
      break;
    }

    case 0x0C: {  // MKDIR: fs|path
      fs::FS* fs = take_fs_path(p, len, 1, path);
      if (!fs) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      fs->mkdir(path) ? proto_reply_ok(seq, cmd) : proto_reply_err(seq, cmd, ST_IO);
      break;
    }

    case 0x0D:  // DF: fs -> total_kb u32|used_kb u32
      if (len < 1 || !fs_of(p[0])) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      reply_df(seq, cmd, p[0]);
      break;

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#else

void fs_handle(uint8_t, uint8_t seq, const uint8_t*, uint16_t) {
  proto_reply_err(seq, CMD(MOD_FS, 0), ST_UNSUPPORTED);
}

#endif
