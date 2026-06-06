// NET: TCP/UDP sockets proxied over serial; per-socket credit windows backpressure TCP instead of dropping.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include <WiFi.h>
#include <WiFiUdp.h>

enum SockType : uint8_t { SK_FREE = 0, SK_TCP, SK_LISTEN, SK_UDP };

struct Sock {
  SockType type = SK_FREE;
  WiFiClient tcp;
  WiFiServer* server = nullptr;
  WiFiUDP udp;
  uint16_t window_used = 0;  // bytes sent to host, unacked
};

static Sock socks[NET_MAX_SOCKETS];

// handle = index + 1 (0 is invalid on the wire)
static int8_t alloc_sock() {
  for (uint8_t i = 0; i < NET_MAX_SOCKETS; i++)
    if (socks[i].type == SK_FREE) return i;
  return -1;
}

static Sock* sock_of(uint8_t handle) {
  if (handle == 0 || handle > NET_MAX_SOCKETS) return nullptr;
  Sock* s = &socks[handle - 1];
  return s->type == SK_FREE ? nullptr : s;
}

static void free_sock(Sock* s) {
  switch (s->type) {
    case SK_TCP:    s->tcp.stop(); break;
    case SK_LISTEN: if (s->server) { s->server->end(); delete s->server; s->server = nullptr; } break;
    case SK_UDP:    s->udp.stop(); break;
    default: break;
  }
  s->type = SK_FREE;
  s->window_used = 0;
}

static void closed_event(uint8_t handle, uint8_t reason) {
  uint8_t buf[2] = { handle, reason };
  proto_send_event(NET_CLOSED_EVT, buf, 2);
}

void net_poll() {
  static uint8_t buf[1 + 6 + NET_CHUNK];  // worst case: UDP header + chunk
  for (uint8_t i = 0; i < NET_MAX_SOCKETS; i++) {
    Sock* s = &socks[i];
    uint8_t handle = i + 1;

    if (s->type == SK_TCP) {
      int avail = s->tcp.available();
      if (avail > 0 && s->window_used < NET_WINDOW) {
        uint16_t room = NET_WINDOW - s->window_used;
        uint16_t n = avail > NET_CHUNK ? NET_CHUNK : (uint16_t)avail;
        if (n > room) n = room;
        if (n > 0) {
          buf[0] = handle;
          int got = s->tcp.read(buf + 1, n);
          if (got > 0) {
            s->window_used += got;
            proto_send_event(NET_DATA_EVT, buf, 1 + got);
          }
        }
      } else if (avail <= 0 && !s->tcp.connected()) {
        closed_event(handle, 0);
        free_sock(s);
      }
    } else if (s->type == SK_LISTEN) {
      WiFiClient c = s->server->accept();
      if (c) {
        int8_t ni = alloc_sock();
        if (ni < 0) {
          c.stop();  // no free slots
        } else {
          socks[ni].type = SK_TCP;
          socks[ni].tcp = c;
          socks[ni].window_used = 0;
          uint8_t ev[8];
          ev[0] = handle;
          ev[1] = ni + 1;
          uint32_t rip = (uint32_t)c.remoteIP();
          memcpy(ev + 2, &rip, 4);
          wr16(ev + 6, c.remotePort());
          proto_send_event(NET_ACCEPT_EVT, ev, 8);
        }
      }
    } else if (s->type == SK_UDP) {
      int n = s->udp.parsePacket();
      if (n > 0) {
        buf[0] = handle;
        uint32_t rip = (uint32_t)s->udp.remoteIP();
        memcpy(buf + 1, &rip, 4);
        wr16(buf + 5, s->udp.remotePort());
        int rd = s->udp.read(buf + 7, n > NET_CHUNK ? NET_CHUNK : n);
        if (rd > 0) proto_send_event(NET_UDP_EVT, buf, 7 + rd);
        s->udp.flush();  // drop tail beyond NET_CHUNK
      }
    }
  }
}

void net_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_NET, op);
  switch (op) {
    case 0x01: {  // TCP_CONNECT: port u16, host str -> handle
      if (len < 4) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      uint16_t port = rd16(p);
      uint8_t hlen = p[2];
      if (len < (uint16_t)(3 + hlen) || hlen == 0) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      char host[128];
      if (hlen >= sizeof(host)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      memcpy(host, p + 3, hlen);
      host[hlen] = 0;
      int8_t i = alloc_sock();
      if (i < 0) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
      if (!socks[i].tcp.connect(host, port, 5000)) {  // blocking, up to 5 s
        proto_reply_err(seq, cmd, ST_SOCKET);
        return;
      }
      socks[i].type = SK_TCP;
      socks[i].window_used = 0;
      uint8_t handle = i + 1;
      proto_reply(seq, cmd, &handle, 1);
      break;
    }

    case 0x02: {  // TCP_LISTEN: port u16 -> handle
      if (len < 2) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      int8_t i = alloc_sock();
      if (i < 0) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
      socks[i].server = new WiFiServer(rd16(p));
      socks[i].server->begin();
      socks[i].type = SK_LISTEN;
      uint8_t handle = i + 1;
      proto_reply(seq, cmd, &handle, 1);
      break;
    }

    case 0x03: {  // UDP_OPEN: local_port u16 -> handle
      if (len < 2) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      int8_t i = alloc_sock();
      if (i < 0) { proto_reply_err(seq, cmd, ST_NO_MEM); return; }
      if (!socks[i].udp.begin(rd16(p))) { proto_reply_err(seq, cmd, ST_SOCKET); return; }
      socks[i].type = SK_UDP;
      uint8_t handle = i + 1;
      proto_reply(seq, cmd, &handle, 1);
      break;
    }

    case 0x04: {  // SEND: handle, data.. -> sent u16 (TCP)
      if (len < 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      Sock* s = sock_of(p[0]);
      if (!s || s->type != SK_TCP) { proto_reply_err(seq, cmd, ST_SOCKET); return; }
      size_t sent = len > 1 ? s->tcp.write(p + 1, len - 1) : 0;
      uint8_t buf[2];
      wr16(buf, (uint16_t)sent);
      proto_reply(seq, cmd, buf, 2);
      break;
    }

    case 0x05: {  // SEND_TO: handle, ip[4], port u16, data.. (UDP)
      if (len < 7) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      Sock* s = sock_of(p[0]);
      if (!s || s->type != SK_UDP) { proto_reply_err(seq, cmd, ST_SOCKET); return; }
      IPAddress ip(p[1], p[2], p[3], p[4]);
      if (!s->udp.beginPacket(ip, rd16(p + 5))) { proto_reply_err(seq, cmd, ST_SOCKET); return; }
      if (len > 7) s->udp.write(p + 7, len - 7);
      if (!s->udp.endPacket()) { proto_reply_err(seq, cmd, ST_SOCKET); return; }
      proto_reply_ok(seq, cmd);
      break;
    }

    case 0x06: {  // CLOSE: handle
      if (len < 1) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      Sock* s = sock_of(p[0]);
      if (s) free_sock(s);
      proto_reply_ok(seq, cmd);  // closing a closed socket is fine
      break;
    }

    case 0x07: {  // WINDOW_ACK: handle, bytes u16 (fire-and-forget)
      if (len < 3) return;
      Sock* s = sock_of(p[0]);
      if (!s) return;
      uint16_t n = rd16(p + 1);
      s->window_used = n >= s->window_used ? 0 : s->window_used - n;
      break;
    }

    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}
