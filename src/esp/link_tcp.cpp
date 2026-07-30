#if defined(ARDUINO_ARCH_ESP32)
// Wi-Fi transport link: the bridge frame stream over a plain TCP socket. Two
// modes, one socket — listen (the host dials in; a few boards with known IPs) or
// dial-home (the board connects OUT to the host and retries forever; the
// mode for hundreds of boards, since the server needs one listening socket and
// DHCP churn and NAT stop mattering).
//
// Raw lwIP sockets, not NetworkClient: the fd is touched by three tasks —
// slow_task (accept/dial/close), rx_task (recv) and tx_task (send) — and an
// int in a std::atomic is the only thing safe to share that way. Concurrent
// send/recv on one fd is fine in lwIP; the close path below is what needs care.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"
#include "espbridge/link.h"
#include "espbridge/radio.h"

#if BRIDGE_WIFI_LINK

#include <WiFi.h>
#include <Preferences.h>
#include <lwip/sockets.h>
#include <lwip/netdb.h>
#include <errno.h>
#include <esp_mac.h>
#include <esp_random.h>
#include <atomic>

// Config, either from the sketch (EspBridge.wifi.begin) or NVS (WIFI_LINK_SETUP).
static char cfg_ssid[33];
static char cfg_pass[65];
static char cfg_srv[65];          // "" = listen mode; else host or host:port
static uint16_t cfg_port = BRIDGE_LINK_PORT;
// Defaults to the library password, not "", so a board provisioned purely over
// the wire is never an open port.
static char cfg_pw[33] = "espbridge";

static bool armed = false;
static bool joined_logged = false;
static int listen_fd = -1;
static int dial_fd = -1;          // non-blocking connect() in progress
static int udp_fd = -1;           // discovery beacon (listen mode)
static std::atomic<int> sock{-1}; // the live peer socket (rx_task/tx_task read this)
static std::atomic<bool> peer_dead{false};
static volatile bool authed_flag = false;
static uint32_t next_try_ms = 0;
static uint16_t attempts = 0;
static uint32_t tx_errors = 0;

static void close_fd(int& fd) {
  if (fd < 0) return;
  lwip_close(fd);
  fd = -1;
}

static void set_nonblocking(int fd) {
  int fl = lwip_fcntl(fd, F_GETFL, 0);
  lwip_fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}

// TCP_NODELAY is not optional: Nagle would hold a small reply for up to 40 ms,
// turning a 2 ms round trip into a 42 ms one. Keepalive reaps a half-open socket
// (host crash) in ~11 s, which would otherwise hold the link's only slot.
static void tune_socket(int fd) {
  int one = 1;
  lwip_setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  lwip_setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));
  int idle = 5, intvl = 2, cnt = 3;
  lwip_setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
  lwip_setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
  lwip_setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof(cnt));
  set_nonblocking(fd);
}

static void adopt_peer(int fd) {
  tune_socket(fd);
  proto_link_tcp_reset();   // no stale half-frame or queued reply from last session
  authed_flag = false;      // every session authenticates again
  sock = fd;
  peer_dead = false;
  attempts = 0;
  proto_log_heap("wifi: peer connected");
}

// slow_task only. Publishing -1 then waiting two ticks means any recv()/send()
// holding the old fd has returned before the number can be recycled.
static void drop_peer() {
  int f = sock.exchange(-1);
  peer_dead = false;
  authed_flag = false;
  if (f < 0) return;
  vTaskDelay(2);
  lwip_close(f);
  proto_link_tcp_reset();
  proto_log(1, "wifi: peer disconnected");
}

// ---- NVS-persisted config ---------------------------------------------------

static void load_config() {
  Preferences prefs;
  if (!prefs.begin("bridge", true)) return;
  prefs.getString("wl_ssid", "").toCharArray(cfg_ssid, sizeof(cfg_ssid));
  prefs.getString("wl_pass", "").toCharArray(cfg_pass, sizeof(cfg_pass));
  prefs.getString("wl_srv", "").toCharArray(cfg_srv, sizeof(cfg_srv));
  cfg_port = prefs.getUShort("wl_port", BRIDGE_LINK_PORT);
  prefs.end();
}

static void save_config() {
  Preferences prefs;
  if (!prefs.begin("bridge", false)) return;
  prefs.putString("wl_ssid", cfg_ssid);
  prefs.putString("wl_pass", cfg_pass);
  prefs.putString("wl_srv", cfg_srv);
  prefs.putUShort("wl_port", cfg_port);
  prefs.end();
}

void link_tcp_forget() {
  Preferences prefs;
  if (!prefs.begin("bridge", false)) return;
  prefs.remove("wl_ssid");
  prefs.remove("wl_pass");
  prefs.remove("wl_srv");
  prefs.remove("wl_port");
  prefs.end();
}

bool link_tcp_configure(const char* ssid, const char* pass, const char* server,
                        uint16_t port, bool persist) {
  if (!ssid || !*ssid) return false;
  strlcpy(cfg_ssid, ssid, sizeof(cfg_ssid));
  strlcpy(cfg_pass, pass ? pass : "", sizeof(cfg_pass));
  strlcpy(cfg_srv, server ? server : "", sizeof(cfg_srv));
  cfg_port = port ? port : BRIDGE_LINK_PORT;
  if (persist) save_config();
  return true;
}

// ---- bring-up ---------------------------------------------------------------

bool link_tcp_begin(const char* ssid, const char* pass, const char* server,
                    uint16_t port, const char* password) {
  if (armed) return true;
  if (password) strlcpy(cfg_pw, password, sizeof(cfg_pw));

  if (ssid && *ssid) {
    strlcpy(cfg_ssid, ssid, sizeof(cfg_ssid));
    strlcpy(cfg_pass, pass ? pass : "", sizeof(cfg_pass));
    strlcpy(cfg_srv, server ? server : "", sizeof(cfg_srv));
    if (port) cfg_port = port;
  } else {
    load_config();  // provisioned earlier over USB/BLE (WIFI_LINK_SETUP)
    if (server && *server) strlcpy(cfg_srv, server, sizeof(cfg_srv));
    if (port) cfg_port = port;
  }
  if (!cfg_ssid[0]) {
    proto_log(2, "wifi link: no credentials (sketch args or WIFI_LINK_SETUP)");
    return false;
  }
  // ESP-NOW pins the radio to a fixed channel, which an AP association cannot
  // survive: mutually exclusive, first one wins.
  if (radio_held(RADIO_ESPNOW)) {
    proto_log(2, "wifi link: refused, ESP-NOW owns the radio");
    return false;
  }
  if (ESP.getFreeHeap() < LINK_TCP_MIN_HEAP) {
    proto_log(2, "wifi link: refused, not enough heap for the Wi-Fi driver");
    return false;
  }
  if (!radio_acquire(RADIO_LINK)) {
    proto_log(2, "wifi link: refused, radio acquire failed");
    return false;
  }
  if (!proto_link_tcp_alloc()) {
    radio_release(RADIO_LINK);
    proto_log(2, "wifi link: refused, no heap for frame buffers");
    return false;
  }

  WiFi.mode(radio_held(RADIO_AP) ? WIFI_MODE_APSTA : WIFI_MODE_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(cfg_ssid, cfg_pass[0] ? cfg_pass : nullptr);
  // Modem sleep adds up to ~100 ms of jitter per round trip, so a control link
  // wants it off — but NEVER with Bluetooth up: WIFI_PS_NONE destabilises the
  // coex arbiter (see mod_wifi.cpp), and a jittery link beats a broken one.
  if (link_ble_enabled()) {
    proto_log(1, "wifi link: BLE is on, keeping modem sleep (higher latency)");
  } else {
    WiFi.setSleep(false);
  }
  armed = true;
  joined_logged = false;
  attempts = 0;
  next_try_ms = 0;
  return true;
}

void link_tcp_stop() {
  if (!armed) return;
  armed = false;
  drop_peer();
  close_fd(listen_fd);
  close_fd(dial_fd);
  close_fd(udp_fd);
  proto_link_tcp_free();
  WiFi.disconnect(false, false);
  radio_release(RADIO_LINK);
  proto_log_heap("wifi link: stopped");
}

// ---- connection management (slow_task) --------------------------------------

static bool start_listen() {
  int fd = lwip_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (fd < 0) return false;
  int one = 1;
  lwip_setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  struct sockaddr_in a = {};
  a.sin_family = AF_INET;
  a.sin_addr.s_addr = htonl(INADDR_ANY);
  a.sin_port = htons(cfg_port);
  if (lwip_bind(fd, (struct sockaddr*)&a, sizeof(a)) < 0 || lwip_listen(fd, 1) < 0) {
    lwip_close(fd);
    return false;
  }
  set_nonblocking(fd);
  listen_fd = fd;
  char msg[64];
  snprintf(msg, sizeof(msg), "wifi link: listening on %s:%u",
           WiFi.localIP().toString().c_str(), (unsigned)cfg_port);
  proto_log(1, msg);
  return true;
}

// "host" or "host:port" -> address; false if the name does not resolve.
static bool resolve_server(struct sockaddr_in& out) {
  char host[64];
  strlcpy(host, cfg_srv, sizeof(host));
  uint16_t port = cfg_port;
  char* colon = strrchr(host, ':');
  if (colon) { *colon = 0; port = (uint16_t)atoi(colon + 1); }

  IPAddress ip;
  if (!ip.fromString(host) && !WiFi.hostByName(host, ip)) return false;
  out = {};
  out.sin_family = AF_INET;
  out.sin_addr.s_addr = (uint32_t)ip;
  out.sin_port = htons(port);
  return true;
}

// Backoff with jitter — the jitter is the point at fleet scale: when the server
// restarts, 1000 boards must not retry on the same millisecond.
static void schedule_retry() {
  attempts++;
  uint32_t wait = (uint32_t)attempts * 1000;
  if (wait > LINK_TCP_RETRY_MAX_MS) wait = LINK_TCP_RETRY_MAX_MS;
  next_try_ms = millis() + wait + (esp_random() % 1000);
}

static void poll_dial() {
  if (dial_fd < 0) {
    if ((int32_t)(millis() - next_try_ms) < 0) return;
    struct sockaddr_in a;
    if (!resolve_server(a)) { schedule_retry(); return; }
    int fd = lwip_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (fd < 0) { schedule_retry(); return; }
    set_nonblocking(fd);
    int r = lwip_connect(fd, (struct sockaddr*)&a, sizeof(a));
    if (r == 0) { dial_fd = -1; adopt_peer(fd); return; }
    if (errno != EINPROGRESS) { lwip_close(fd); schedule_retry(); return; }
    dial_fd = fd;
    next_try_ms = millis() + 10000;  // connect deadline
    return;
  }
  // A connect in flight completes by becoming writable; SO_ERROR says whether
  // it succeeded or was refused.
  fd_set w;
  FD_ZERO(&w);
  FD_SET(dial_fd, &w);
  struct timeval tv = {0, 0};
  int n = lwip_select(dial_fd + 1, nullptr, &w, nullptr, &tv);
  if (n > 0) {
    int err = 0;
    socklen_t elen = sizeof(err);
    lwip_getsockopt(dial_fd, SOL_SOCKET, SO_ERROR, &err, &elen);
    int fd = dial_fd;
    dial_fd = -1;
    if (err == 0) { adopt_peer(fd); return; }
    lwip_close(fd);
    schedule_retry();
  } else if ((int32_t)(millis() - next_try_ms) >= 0) {
    close_fd(dial_fd);   // connect deadline blew past
    schedule_retry();
  }
}

// Discovery beacon (listen mode only): answer an "ESPB?" UDP broadcast with
// "ESPB!" | mac[6] | port u16 | name[], so a host finds boards without anyone
// writing down IPs. Same identity the BLE advertisement carries, and the name
// needs no length byte — the datagram bounds it.
static void poll_beacon() {
  if (udp_fd < 0) {
    udp_fd = lwip_socket(AF_INET, SOCK_DGRAM, 0);
    if (udp_fd < 0) return;
    int one = 1;
    lwip_setsockopt(udp_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in a = {};
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_ANY);
    a.sin_port = htons(cfg_port);
    if (lwip_bind(udp_fd, (struct sockaddr*)&a, sizeof(a)) < 0) {
      lwip_close(udp_fd);
      udp_fd = -1;
      return;
    }
    set_nonblocking(udp_fd);
  }
  uint8_t req[16];
  struct sockaddr_in from;
  socklen_t flen = sizeof(from);
  int n = lwip_recvfrom(udp_fd, req, sizeof(req), 0, (struct sockaddr*)&from, &flen);
  if (n < 5 || memcmp(req, "ESPB?", 5) != 0) return;
  uint8_t rep[13 + BRIDGE_NAME_MAX];
  memcpy(rep, "ESPB!", 5);
  esp_read_mac(rep + 5, ESP_MAC_WIFI_STA);
  rep[11] = (uint8_t)(cfg_port >> 8);
  rep[12] = (uint8_t)cfg_port;
  const char* nm = sys_device_name();
  size_t nl = strnlen(nm, BRIDGE_NAME_MAX);
  memcpy(rep + 13, nm, nl);
  lwip_sendto(udp_fd, rep, 13 + nl, 0, (struct sockaddr*)&from, flen);
}

static void poll_accept() {
  if (listen_fd < 0) {
    if ((int32_t)(millis() - next_try_ms) < 0) return;  // failing bind: back off
    if (!start_listen()) { schedule_retry(); return; }
    attempts = 0;
  }
  struct sockaddr_in from;
  socklen_t flen = sizeof(from);
  int fd = lwip_accept(listen_fd, (struct sockaddr*)&from, &flen);
  if (fd < 0) return;
  if (sock.load() >= 0) {
    // One peer at a time, newcomer wins: a crashed host leaves a half-open
    // socket, and refusing new connections would lock the board out until
    // keepalive reaps it.
    proto_log(1, "wifi link: replacing previous peer");
    drop_peer();
  }
  adopt_peer(fd);
}

// A provisioned board must come back on Wi-Fi after every reboot without the
// sketch knowing anything about it. Deferred 2 s so a sketch's own ble.begin()
// claims its heap first — and so an explicit wifi.begin() makes this a no-op.
static void autostart_from_nvs() {
  static bool done = false;
  if (done || armed || millis() < 2000) return;
  done = true;
  load_config();
  if (!cfg_ssid[0]) return;
  proto_log(1, "wifi link: starting from stored credentials");
  link_tcp_begin(nullptr, nullptr, nullptr, 0,
                 link_ble_enabled() ? link_ble_password() : nullptr);
}

void link_tcp_poll() {
  if (!armed) { autostart_from_nvs(); return; }
  if (peer_dead.load()) drop_peer();

  if (WiFi.status() != WL_CONNECTED) {
    if (sock.load() >= 0) drop_peer();
    close_fd(listen_fd);
    close_fd(dial_fd);
    close_fd(udp_fd);
    joined_logged = false;   // WiFi.setAutoReconnect handles rejoining
    return;
  }
  if (!joined_logged) { joined_logged = true; proto_log_heap("wifi link: joined"); }
  if (cfg_srv[0]) {
    if (sock.load() < 0) poll_dial();
  } else {
    poll_beacon();                       // answerable even with a peer connected
    if (sock.load() < 0) poll_accept();
  }
}

// ---- data path (rx_task / tx_task) ------------------------------------------

// "Try again later", not "this connection is finished". ENOMEM is the one that
// matters: under a pipelined burst lwIP runs out of pbufs and lwip_send() fails
// with ENOMEM, not EWOULDBLOCK. Treating that as a dead peer drops the link
// exactly when the host is busiest.
static bool transient_errno() {
  return errno == EWOULDBLOCK || errno == EAGAIN || errno == ENOMEM
         || errno == EINTR;
}

uint16_t link_tcp_read(uint8_t* buf, uint16_t maxlen) {
  int f = sock.load();
  if (f < 0 || peer_dead.load()) return 0;
  int n = lwip_recv(f, buf, maxlen, 0);
  if (n > 0) return (uint16_t)n;
  if (n == 0 || !transient_errno()) peer_dead = true;  // 0 = orderly close
  return 0;
}

uint16_t link_tcp_write_chunk(const uint8_t* data, uint16_t len) {
  int f = sock.load();
  if (f < 0 || peer_dead.load()) return 0;
  int n = lwip_send(f, data, len, 0);
  if (n > 0) return (uint16_t)n;
  if (n < 0 && !transient_errno()) {
    peer_dead = true;
    tx_errors++;
  }
  return 0;  // send buffer full: tx_task retries on the next pass
}

// ---- status -----------------------------------------------------------------

bool link_tcp_enabled() { return armed; }
bool link_tcp_up() { return sock.load() >= 0 && !peer_dead.load(); }
bool link_tcp_authed() { return link_tcp_up() && authed_flag; }
void link_tcp_set_authed(bool v) { authed_flag = v; }
const char* link_tcp_password() { return cfg_pw; }
uint32_t link_tcp_tx_errors() { return tx_errors; }
// Derived, not tracked: a mirrored variable is one more thing to forget.
uint8_t link_tcp_state() {
  if (!armed) return 0;                              // off
  if (WiFi.status() != WL_CONNECTED) return 1;       // joining
  return sock.load() >= 0 ? 3 : 2;                   // peer / listening-dialing
}
uint16_t link_tcp_port() { return cfg_port; }
uint32_t link_tcp_ip() { return armed ? (uint32_t)WiFi.localIP() : 0; }

#else  // !BRIDGE_WIFI_LINK — stubs so the rest of the tree links unchanged

bool link_tcp_begin(const char*, const char*, const char*, uint16_t, const char*) { return false; }
void link_tcp_stop() {}
bool link_tcp_enabled() { return false; }
bool link_tcp_up() { return false; }
bool link_tcp_authed() { return false; }
void link_tcp_set_authed(bool) {}
const char* link_tcp_password() { return ""; }
uint32_t link_tcp_tx_errors() { return 0; }
uint8_t link_tcp_state() { return 0; }
uint16_t link_tcp_port() { return 0; }
uint32_t link_tcp_ip() { return 0; }
void link_tcp_poll() {}
uint16_t link_tcp_write_chunk(const uint8_t*, uint16_t) { return 0; }
uint16_t link_tcp_read(uint8_t*, uint16_t) { return 0; }

#endif  // BRIDGE_WIFI_LINK
#endif  // ARDUINO_ARCH_ESP32
