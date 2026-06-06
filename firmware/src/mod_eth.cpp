// Ethernet (compile-time opt-in: BRIDGE_ENABLE_ETH in firmware.ino).
// Thin bridge over ETH.h: bring the PHY up with parameters the host sends
// (board presets live in Python — espbridge.eth.PRESETS). Once link + IP are
// up, the existing NET sockets route over Ethernet automatically because
// ETHClass is a NetworkInterface in the unified core-3.x Network stack.
#include "espbridge/protocol.h"
#include "espbridge/modules.h"

#if BRIDGE_ETH

#include <ETH.h>

static bool eth_started = false;

// Wire phy id -> eth_phy_type_t (the core enum's values shift with chip
// config, so the protocol uses stable ids; see commands.h).
static bool phy_of(uint8_t id, eth_phy_type_t* out) {
  switch (id) {
#if defined(CONFIG_ETH_USE_ESP32_EMAC)
    case 0: *out = ETH_PHY_GENERIC; return true;
    case 1: *out = ETH_PHY_LAN8720; return true;
    case 2: *out = ETH_PHY_TLK110; return true;
    case 3: *out = ETH_PHY_RTL8201; return true;
    case 4: *out = ETH_PHY_DP83848; return true;
    case 5: *out = ETH_PHY_KSZ8041; return true;
    case 6: *out = ETH_PHY_KSZ8081; return true;
#endif
#if CONFIG_ETH_SPI_ETHERNET_DM9051
    case 16: *out = ETH_PHY_DM9051; return true;
#endif
#if CONFIG_ETH_SPI_ETHERNET_W5500
    case 17: *out = ETH_PHY_W5500; return true;
#endif
#if CONFIG_ETH_SPI_ETHERNET_KSZ8851SNL
    case 18: *out = ETH_PHY_KSZ8851; return true;
#endif
    default: return false;
  }
}

static void on_eth_event(arduino_event_id_t event, arduino_event_info_t info) {
  uint8_t buf[5] = {0};
  switch (event) {
    case ARDUINO_EVENT_ETH_CONNECTED:
      buf[0] = 1;
      break;
    case ARDUINO_EVENT_ETH_GOT_IP: {
      buf[0] = 2;
      uint32_t ip = (uint32_t)ETH.localIP();
      memcpy(buf + 1, &ip, 4);
      break;
    }
    case ARDUINO_EVENT_ETH_DISCONNECTED:
      buf[0] = 3;
      break;
    default:
      return;
  }
  proto_send_event(ETH_STATE_EVT, buf, 5);
}

void eth_handle(uint8_t op, uint8_t seq, const uint8_t* p, uint16_t len) {
  uint16_t cmd = CMD(MOD_ETH, op);
  switch (op) {
    case 0x01: {  // BEGIN_RMII: phy|addr|mdc|mdio|power|clk (classic ESP32 EMAC)
#if defined(CONFIG_ETH_USE_ESP32_EMAC)
      eth_phy_type_t phy;
      if (len < 6 || !phy_of(p[0], &phy)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (eth_started) { proto_reply_err(seq, cmd, ST_BUSY); return; }
      Network.onEvent(on_eth_event);
      if (!ETH.begin(phy, (int8_t)p[1], (int8_t)p[2],
                     (int8_t)p[3], (int8_t)p[4], (eth_clock_mode_t)p[5])) {
        proto_reply_err(seq, cmd, ST_IO);
        return;
      }
      eth_started = true;
      proto_reply_ok(seq, cmd);
#else
      proto_reply_err(seq, cmd, ST_UNSUPPORTED);  // no EMAC on this chip
#endif
      break;
    }
    case 0x02: {  // BEGIN_SPI: phy|addr|cs|irq|rst|sck|miso|mosi|freq_mhz
      eth_phy_type_t phy;
      if (len < 9 || !phy_of(p[0], &phy)) { proto_reply_err(seq, cmd, ST_BAD_ARGS); return; }
      if (eth_started) { proto_reply_err(seq, cmd, ST_BUSY); return; }
      Network.onEvent(on_eth_event);
#if SOC_SPI_PERIPH_NUM > 2
      spi_host_device_t host = SPI3_HOST;
#else
      spi_host_device_t host = SPI2_HOST;
#endif
      if (!ETH.begin(phy, (int8_t)p[1], (int8_t)p[2],
                     (int8_t)p[3], (int8_t)p[4], host, (int8_t)p[5],
                     (int8_t)p[6], (int8_t)p[7], p[8])) {
        proto_reply_err(seq, cmd, ST_IO);
        return;
      }
      eth_started = true;
      proto_reply_ok(seq, cmd);
      break;
    }
    case 0x03:  // STOP
      if (eth_started) {
        ETH.end();
        eth_started = false;
      }
      proto_reply_ok(seq, cmd);
      break;
    case 0x04: {  // STATUS -> link u8|ip[4]|gw[4]|mask[4]|mac[6]
      uint8_t buf[19] = {0};
      buf[0] = eth_started && ETH.linkUp() ? 1 : 0;
      uint32_t ip = (uint32_t)ETH.localIP();
      uint32_t gw = (uint32_t)ETH.gatewayIP();
      uint32_t mask = (uint32_t)ETH.subnetMask();
      memcpy(buf + 1, &ip, 4);
      memcpy(buf + 5, &gw, 4);
      memcpy(buf + 9, &mask, 4);
      ETH.macAddress(buf + 13);
      proto_reply(seq, cmd, buf, 19);
      break;
    }
    default:
      proto_reply_err(seq, cmd, ST_UNKNOWN_CMD);
  }
}

#else

void eth_handle(uint8_t, uint8_t seq, const uint8_t*, uint16_t) {
  proto_reply_err(seq, CMD(MOD_ETH, 0), ST_UNSUPPORTED);
}

#endif
