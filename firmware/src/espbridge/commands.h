// python-esp-bridge — shared protocol contract.
// MUST stay in sync with the Python package: src/espbridge/constants.py (repo root).
#pragma once
#include <stdint.h>

#define PROTOCOL_VERSION 1
#define FW_VERSION_MAJOR 0
#define FW_VERSION_MINOR 2
#define FW_VERSION_PATCH 0

// Frame (logical, pre-COBS):
//   flags u8 | seq u8 | cmd u16 BE | payload .. | crc16 BE
// COBS-encoded on the wire, frames delimited by 0x00.
// crc16 = CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over flags..payload.
#define FLAG_EVENT 0x01  // async event (not a response)
#define FLAG_ERROR 0x02  // response is an error; payload[0] = status
#define FLAG_MORE  0x04  // reserved (fragmentation)

#define MAX_PAYLOAD 2048
#define NET_CHUNK   512   // max bytes per NET data event
#define UART_CHUNK  256   // max bytes per UART rx event

// seq 0 = fire-and-forget (no response expected); host uses 1..255.

// ---- status codes -----------------------------------------------------------
enum Status : uint8_t {
  ST_OK          = 0x00,
  ST_UNKNOWN_CMD = 0x01,
  ST_BAD_ARGS    = 0x02,
  ST_UNSUPPORTED = 0x03,
  ST_BUSY        = 0x04,
  ST_TIMEOUT     = 0x05,
  ST_NO_MEM      = 0x06,
  ST_BAD_PIN     = 0x07,
  ST_NOT_INIT    = 0x08,
  ST_IO          = 0x09,
  ST_WIFI        = 0x0A,
  ST_SOCKET      = 0x0B,
  ST_CRC         = 0x0C,
  ST_DENIED      = 0x0D,  // wireless link not authenticated (see SYS_AUTH)
};

// ---- capability bits (SYS_INFO.caps u32) ------------------------------------
#define CAP_WIFI       (1UL << 0)
#define CAP_BLE        (1UL << 1)
#define CAP_BT_CLASSIC (1UL << 2)
#define CAP_DAC        (1UL << 3)
#define CAP_TOUCH      (1UL << 4)
#define CAP_HALL       (1UL << 5)
#define CAP_PSRAM      (1UL << 6)
#define CAP_NATIVE_USB (1UL << 7)
#define CAP_BLE_FW     (1UL << 8)   // firmware compiled with BLE support
#define CAP_BLE_LINK   (1UL << 9)   // bridge reachable over the BLE transport
#define CAP_ESPNOW     (1UL << 10)  // ESP-NOW connectionless messaging

// ---- chip models -------------------------------------------------------------
enum ChipModel : uint8_t {
  CHIP_UNKNOWN = 0, CHIP_ESP32 = 1, CHIP_ESP32S2 = 2, CHIP_ESP32S3 = 3,
  CHIP_ESP32C3 = 4, CHIP_ESP32C6 = 5, CHIP_ESP32H2 = 6,
};

// ---- command ids: cmd = (MODULE << 8) | OP ----------------------------------
// ops 0x00..0x7F: host->fw requests; ops 0x80..0xFF: fw->host events.
#define MOD_SYS   0x00
#define MOD_GPIO  0x10
#define MOD_ADC   0x20
#define MOD_DAC   0x21
#define MOD_TOUCH 0x22
#define MOD_PWM   0x30
#define MOD_I2C   0x40
#define MOD_SPI   0x41
#define MOD_UART  0x42
#define MOD_WIFI  0x50
#define MOD_NET   0x51
#define MOD_ESPNOW 0x52
#define MOD_BLE   0x60

#define CMD(mod, op) ((uint16_t)(((mod) << 8) | (op)))

// SYS
#define SYS_PING        CMD(MOD_SYS, 0x01)  // payload echoed back
#define SYS_INFO        CMD(MOD_SYS, 0x02)  // -> proto u8|fw[3]|model u8|rev u8|mac[6]|caps u32|gpio_count u8|flash_mb u8|name_len u8|name
#define SYS_SET_BAUD    CMD(MOD_SYS, 0x03)  // baud u32 (reply sent at old baud, then switch)
#define SYS_RESET       CMD(MOD_SYS, 0x04)
#define SYS_FREE_HEAP   CMD(MOD_SYS, 0x05)  // -> free u32|min_free u32|largest u32|dropped_events u32
#define SYS_SET_NAME    CMD(MOD_SYS, 0x06)  // payload = device name (0..32 bytes), persisted in NVS
#define SYS_AUTH        CMD(MOD_SYS, 0x07)  // payload = password; required over BLE before any other cmd
#define SYS_READY       CMD(MOD_SYS, 0x80)  // event at boot; payload = same as SYS_INFO
#define SYS_LOG         CMD(MOD_SYS, 0x81)  // event: level u8|msg

#define BRIDGE_NAME_MAX 32

// GPIO
#define GPIO_SET_MODE   CMD(MOD_GPIO, 0x01) // pin u8|mode u8 (0 in,1 out,2 in_pullup,3 in_pulldown,4 out_open_drain)
#define GPIO_WRITE      CMD(MOD_GPIO, 0x02) // pin u8|value u8
#define GPIO_READ       CMD(MOD_GPIO, 0x03) // pin u8 -> value u8
#define GPIO_WRITE_MASK CMD(MOD_GPIO, 0x04) // mask u64|values u64 (BE)
#define GPIO_READ_ALL   CMD(MOD_GPIO, 0x05) // -> levels u64 BE
#define GPIO_WATCH      CMD(MOD_GPIO, 0x06) // pin u8|edge u8 (1 rise,2 fall,3 change)|debounce_ms u16
#define GPIO_UNWATCH    CMD(MOD_GPIO, 0x07) // pin u8
#define GPIO_EDGE_EVT   CMD(MOD_GPIO, 0x80) // pin u8|level u8|millis u32

// ADC
#define ADC_CONFIG      CMD(MOD_ADC, 0x01)  // pin u8|atten u8 (0:0dB 1:2.5dB 2:6dB 3:11dB)
#define ADC_READ        CMD(MOD_ADC, 0x02)  // pin u8 -> raw u16 (12-bit)
#define ADC_READ_MV     CMD(MOD_ADC, 0x03)  // pin u8 -> millivolts u16

// DAC (classic ESP32 only: GPIO25/26)
#define DAC_WRITE       CMD(MOD_DAC, 0x01)  // pin u8|value u8
#define DAC_COSINE      CMD(MOD_DAC, 0x02)  // pin u8|freq u32|scale u8(0..3)|offset i8|phase u8(0|1=180deg)
#define DAC_COS_STOP    CMD(MOD_DAC, 0x03)  // pin u8
#define DAC_DISABLE     CMD(MOD_DAC, 0x04)  // pin u8

// TOUCH
#define TOUCH_READ      CMD(MOD_TOUCH, 0x01) // pin u8 -> value u32 (classic: lower=touch, S3: higher=touch)

// PWM (LEDC)
#define PWM_ATTACH      CMD(MOD_PWM, 0x01)  // pin u8|freq u32|res_bits u8
#define PWM_WRITE       CMD(MOD_PWM, 0x02)  // pin u8|duty u32
#define PWM_DETACH      CMD(MOD_PWM, 0x03)  // pin u8
#define PWM_TONE        CMD(MOD_PWM, 0x04)  // pin u8|freq u32 (0 = off)

// I2C
#define I2C_INIT        CMD(MOD_I2C, 0x01)  // bus u8|sda u8|scl u8|freq u32
#define I2C_SCAN        CMD(MOD_I2C, 0x02)  // bus u8 -> n u8|addr[n]
#define I2C_WRITE       CMD(MOD_I2C, 0x03)  // bus u8|addr u8|data..
#define I2C_READ        CMD(MOD_I2C, 0x04)  // bus u8|addr u8|len u8 -> data
#define I2C_WRITE_READ  CMD(MOD_I2C, 0x05)  // bus u8|addr u8|wlen u8|wdata|rlen u8 -> data (repeated start)
#define I2C_DEINIT      CMD(MOD_I2C, 0x06)  // bus u8

// SPI
#define SPI_INIT        CMD(MOD_SPI, 0x01)  // host u8|sck i8|miso i8|mosi i8|freq u32|mode u8|msb_first u8
#define SPI_TRANSFER    CMD(MOD_SPI, 0x02)  // host u8|cs i8|data.. -> rx data (full duplex)
#define SPI_DEINIT      CMD(MOD_SPI, 0x03)  // host u8

// UART (secondary ports 1,2)
#define UART_INIT       CMD(MOD_UART, 0x01) // port u8|tx i8|rx i8|baud u32
#define UART_WRITE      CMD(MOD_UART, 0x02) // port u8|data..
#define UART_DEINIT     CMD(MOD_UART, 0x03) // port u8
#define UART_RX_EVT     CMD(MOD_UART, 0x80) // port u8|data..

// WIFI
#define WIFI_SCAN       CMD(MOD_WIFI, 0x01) // (async; results via events)
#define WIFI_CONNECT    CMD(MOD_WIFI, 0x02) // ssid_len u8|ssid|pass_len u8|pass
#define WIFI_DISCONNECT CMD(MOD_WIFI, 0x03)
#define WIFI_STATUS     CMD(MOD_WIFI, 0x04) // -> status u8|ip[4]|gw[4]|mask[4]|rssi i8|channel u8|mac[6]
#define WIFI_AP_START   CMD(MOD_WIFI, 0x05) // ssid_len u8|ssid|pass_len u8|pass|channel u8|max_conn u8 -> ip[4]
#define WIFI_AP_STOP    CMD(MOD_WIFI, 0x06)
#define WIFI_HOSTNAME   CMD(MOD_WIFI, 0x07) // name str
#define WIFI_STATE_EVT  CMD(MOD_WIFI, 0x80) // event u8 (1 connected,2 got_ip,3 disconnected)|ip[4]
#define WIFI_SCAN_RES   CMD(MOD_WIFI, 0x81) // idx u8|total u8|rssi i8|auth u8|channel u8|bssid[6]|ssid_len u8|ssid
#define WIFI_SCAN_DONE  CMD(MOD_WIFI, 0x82) // count u8

// NET (sockets proxied through the ESP32 radio)
#define NET_TCP_CONNECT CMD(MOD_NET, 0x01)  // port u16|host str -> handle u8
#define NET_TCP_LISTEN  CMD(MOD_NET, 0x02)  // port u16 -> handle u8
#define NET_UDP_OPEN    CMD(MOD_NET, 0x03)  // local_port u16 -> handle u8
#define NET_SEND        CMD(MOD_NET, 0x04)  // handle u8|data.. -> sent u16
#define NET_SEND_TO     CMD(MOD_NET, 0x05)  // handle u8|ip[4]|port u16|data.. (UDP)
#define NET_CLOSE       CMD(MOD_NET, 0x06)  // handle u8
#define NET_WINDOW_ACK  CMD(MOD_NET, 0x07)  // handle u8|bytes u16 (fire-and-forget, seq=0)
#define NET_DATA_EVT    CMD(MOD_NET, 0x80)  // handle u8|data.. (TCP)
#define NET_ACCEPT_EVT  CMD(MOD_NET, 0x81)  // listen_h u8|new_h u8|ip[4]|port u16
#define NET_CLOSED_EVT  CMD(MOD_NET, 0x82)  // handle u8|reason u8
#define NET_UDP_EVT     CMD(MOD_NET, 0x83)  // handle u8|ip[4]|port u16|data..

// ESP-NOW (connectionless 2.4 GHz messaging; coexists with Wi-Fi STA/AP + BLE).
// Init order on classic ESP32: Wi-Fi -> ESP-NOW -> BLE (see BRIDGE_WIFI_COEX).
// Channel rule: when Wi-Fi STA is connected ESP-NOW inherits its channel and
// the requested channel is ignored (changing it would drop the AP).
#define ESPNOW_INIT     CMD(MOD_ESPNOW, 0x01) // channel u8 (0=auto/inherit)|flags u8 (bit0=long range) -> mac[6]
#define ESPNOW_DEINIT   CMD(MOD_ESPNOW, 0x02)
#define ESPNOW_SET_PMK  CMD(MOD_ESPNOW, 0x03) // pmk[16] (global key for encrypted peers)
#define ESPNOW_ADD_PEER CMD(MOD_ESPNOW, 0x04) // mac[6]|channel u8 (0=follow)|encrypt u8|[lmk[16]]
#define ESPNOW_DEL_PEER CMD(MOD_ESPNOW, 0x05) // mac[6]
#define ESPNOW_SEND     CMD(MOD_ESPNOW, 0x06) // mac[6]|data (<=250). seq!=0 -> delivered u8 (1=peer ACKed);
                                              // seq==0: fire-and-forget, result via ESPNOW_SEND_EVT
#define ESPNOW_RX_EVT   CMD(MOD_ESPNOW, 0x80) // src_mac[6]|rssi i8|data..
#define ESPNOW_SEND_EVT CMD(MOD_ESPNOW, 0x81) // dst_mac[6]|status u8 (seq==0 sends only; best-effort)

#define ESPNOW_MAX_DATA 250  // ESP_NOW_MAX_DATA_LEN

// BLE (UUIDs always 16 bytes / 128-bit on the wire; Python expands 16-bit UUIDs)
#define BLE_SCAN_START  CMD(MOD_BLE, 0x01)  // duration_s u8 (0=forever)|active u8
#define BLE_SCAN_STOP   CMD(MOD_BLE, 0x02)
#define BLE_ADV_START   CMD(MOD_BLE, 0x03)  // name_len u8|name|mfg_len u8|mfg|svc_uuid16 u16 (0=none)
#define BLE_ADV_STOP    CMD(MOD_BLE, 0x04)
#define BLE_GATTS_DEF   CMD(MOD_BLE, 0x05)  // svc_uuid[16]|n u8|{uuid[16]|props u8}*n -> char ids u8[n]
#define BLE_GATTS_SET   CMD(MOD_BLE, 0x06)  // char_id u8|data..
#define BLE_GATTS_NTFY  CMD(MOD_BLE, 0x07)  // char_id u8|data..
#define BLE_GATTC_CONN  CMD(MOD_BLE, 0x08)  // addr[6]|addr_type u8
#define BLE_GATTC_DISC  CMD(MOD_BLE, 0x09)
#define BLE_GATTC_READ  CMD(MOD_BLE, 0x0A)  // svc_uuid[16]|chr_uuid[16] -> data
#define BLE_GATTC_WRITE CMD(MOD_BLE, 0x0B)  // svc_uuid[16]|chr_uuid[16]|data..
#define BLE_GATTC_SUB   CMD(MOD_BLE, 0x0C)  // svc_uuid[16]|chr_uuid[16]|enable u8
#define BLE_ADV_EVT     CMD(MOD_BLE, 0x80)  // addr[6]|addr_type u8|rssi i8|payload..
#define BLE_GATTS_WR_EVT CMD(MOD_BLE, 0x81) // char_id u8|data..
#define BLE_GATTS_CONN_EVT CMD(MOD_BLE, 0x82) // connected u8
#define BLE_GATTC_NTFY_EVT CMD(MOD_BLE, 0x83) // chr_uuid[16]|data..
#define BLE_GATTC_DISC_EVT CMD(MOD_BLE, 0x84) // (disconnected)

// GATT characteristic property bits (BLE_GATTS_DEF props)
#define GATT_PROP_READ   0x01
#define GATT_PROP_WRITE  0x02
#define GATT_PROP_NOTIFY 0x04
#define GATT_PROP_WRITE_NR 0x08
