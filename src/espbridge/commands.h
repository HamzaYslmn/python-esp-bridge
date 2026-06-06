// python-esp-bridge — shared protocol contract.
// MUST stay in sync with the Python package: src/espbridge/constants.py (repo root).
#pragma once
#include <stdint.h>

#define PROTOCOL_VERSION 1
#define FW_VERSION_MAJOR 0
#define FW_VERSION_MINOR 3
#define FW_VERSION_PATCH 3

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
  ST_NOT_FOUND   = 0x0E,  // no such key/path/peripheral instance
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
#define CAP_RMT        (1UL << 11)  // generic pulse-train play/capture (RMT)
#define CAP_ONEWIRE    (1UL << 12)  // 1-Wire bit-timing primitives
#define CAP_TWAI       (1UL << 13)  // CAN bus (TWAI controller)
#define CAP_I2S        (1UL << 14)  // I2S audio in/out
#define CAP_FS         (1UL << 15)  // filesystem access (LittleFS/SD)
#define CAP_NVS        (1UL << 16)  // persistent key/value store
#define CAP_OTA        (1UL << 17)  // firmware update over the link
#define CAP_ETH        (1UL << 18)  // Ethernet (compile-time opt-in)
#define CAP_CAM        (1UL << 19)  // camera (compile-time opt-in, needs PSRAM)
#define CAP_MCPWM      (1UL << 20)  // complementary PWM pair with deadtime
#define CAP_SLEEP      (1UL << 21)  // deep/light sleep (IRAM-gated off on classic+BLE)

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
#define MOD_RMT   0x31
#define MOD_MCPWM 0x33
#define MOD_I2C   0x40
#define MOD_SPI   0x41
#define MOD_UART  0x42
#define MOD_ONEWIRE 0x43
#define MOD_TWAI  0x44
#define MOD_I2S   0x45
#define MOD_WIFI  0x50
#define MOD_NET   0x51
#define MOD_ESPNOW 0x52
#define MOD_ETH   0x53
#define MOD_BLE   0x60
#define MOD_FS    0x70
#define MOD_NVS   0x71
#define MOD_OTA   0x72
#define MOD_CAM   0x73

#define CMD(mod, op) ((uint16_t)(((mod) << 8) | (op)))

// SYS
#define SYS_PING        CMD(MOD_SYS, 0x01)  // payload echoed back
#define SYS_INFO        CMD(MOD_SYS, 0x02)  // -> proto u8|fw[3]|model u8|rev u8|mac[6]|caps u32|gpio_count u8|flash_mb u8|name_len u8|name
#define SYS_SET_BAUD    CMD(MOD_SYS, 0x03)  // baud u32 (reply sent at old baud, then switch)
#define SYS_RESET       CMD(MOD_SYS, 0x04)
#define SYS_FREE_HEAP   CMD(MOD_SYS, 0x05)  // -> free u32|min_free u32|largest u32|dropped_events u32
#define SYS_SET_NAME    CMD(MOD_SYS, 0x06)  // payload = device name (0..32 bytes), persisted in NVS
#define SYS_AUTH        CMD(MOD_SYS, 0x07)  // payload = password; required over BLE before any other cmd
#define SYS_SLEEP       CMD(MOD_SYS, 0x08)  // mode u8 (0 deep,1 light)|us u64|wake_pin i8 (-1 none)|wake_level u8
                                            // deep: replies OK, flushes, sleeps (link drops; board reboots on wake)
                                            // light: replies AFTER waking -> cause u8
#define SYS_WAKE_CAUSE  CMD(MOD_SYS, 0x09)  // -> cause u8 (esp_sleep_wakeup_cause_t: 0 power-on/reset, 2 ext0, 3 ext1, 4 timer, 7 gpio)
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

// RMT — generic pulse-train play/capture. The host implements device
// protocols (WS2812, IR, DHT, HC-SR04, steppers, ...) on top of these.
// A symbol is u16 BE: level<<15 | duration (ticks, 1..0x7FFF); tick period
// is set by RMT_INIT tick_hz (e.g. 1 MHz = 1 us ticks, 10 MHz = 100 ns).
#define RMT_INIT        CMD(MOD_RMT, 0x01)  // pin u8|dir u8 (0 tx,1 rx)|tick_hz u32
#define RMT_DEINIT      CMD(MOD_RMT, 0x02)  // pin u8
#define RMT_TX          CMD(MOD_RMT, 0x03)  // pin u8|sym u16[..] (blocks until sent)
#define RMT_TX_BYTES    CMD(MOD_RMT, 0x04)  // pin u8|bit0 u32|bit1 u32|data..
                                            // bitN = sym pair (first u16 << 16 | second u16); each data
                                            // byte expands MSB-first to 8 symbol pairs (WS2812 et al.)
#define RMT_TX_LOOP     CMD(MOD_RMT, 0x05)  // pin u8|sym u16[..] (repeats until TX_STOP)
#define RMT_TX_STOP     CMD(MOD_RMT, 0x06)  // pin u8
#define RMT_RECV        CMD(MOD_RMT, 0x07)  // pin u8|idle_thresh u16 (ticks)|timeout_ms u16|max_syms u16|
                                            // trig_pin u8 (0xFF none; may equal pin)|trig_level u8|trig_us u32
                                            // -> sym u16[..]; arms RX, fires optional trigger pulse, waits
#define RMT_CARRIER     CMD(MOD_RMT, 0x08)  // pin u8|freq u32|duty_pct u8|enable u8 (IR 38 kHz modulation)

#define RMT_MAX_RX_SYMS 1020  // capture cap per RMT_RECV reply (2 bytes each)

// MCPWM — complementary PWM pair with hardware deadtime (H-bridge drivers).
// Not on ESP32-S2/C3 (no MCPWM peripheral).
#define MCPWM_INIT      CMD(MOD_MCPWM, 0x01) // pin_a u8|pin_b i8 (-1 = single)|freq u32|deadtime_ns u16
#define MCPWM_DUTY      CMD(MOD_MCPWM, 0x02) // permille u16 (0..1000)
#define MCPWM_STOP      CMD(MOD_MCPWM, 0x03)

// I2C
#define I2C_INIT        CMD(MOD_I2C, 0x01)  // bus u8|sda u8|scl u8|freq u32 -> wire_buf u16 (>=0.3.0)
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

// ONEWIRE — 1-Wire bit-timing primitives only. ROM search, CRC8 and device
// drivers (DS18B20, ...) live on the host.
#define OW_RESET        CMD(MOD_ONEWIRE, 0x01) // pin u8 -> present u8
#define OW_WRITE        CMD(MOD_ONEWIRE, 0x02) // pin u8|power u8|data.. (power=1: drive bus high after, parasite supply)
#define OW_READ         CMD(MOD_ONEWIRE, 0x03) // pin u8|n u8 -> data[n]
#define OW_TRIPLET      CMD(MOD_ONEWIRE, 0x04) // pin u8|dir u8 -> id_bit u8|cmp_bit u8|taken u8 (search step)

// TWAI (CAN bus; needs an external transceiver). Filter is part of INIT
// because the IDF driver only accepts it at install time.
#define TWAI_INIT       CMD(MOD_TWAI, 0x01) // tx u8|rx u8|mode u8 (0 normal,1 listen,2 no_ack)|baud u8|
                                            // [code u32|mask u32|single u8] (acceptance filter, optional)
                                            // baud: 0:25k 1:50k 2:100k 3:125k 4:250k 5:500k 6:800k 7:1M
#define TWAI_SEND       CMD(MOD_TWAI, 0x02) // flags u8 (bit0 extd, bit1 rtr)|id u32|data 0..8
#define TWAI_STATUS     CMD(MOD_TWAI, 0x03) // -> state u8 (0 stopped,1 running,2 recovering,3 bus_off)|
                                            //    tx_err u8|rx_err u8|rx_missed u32
#define TWAI_RECOVER    CMD(MOD_TWAI, 0x04) // start bus-off recovery
#define TWAI_DEINIT     CMD(MOD_TWAI, 0x05)
#define TWAI_RX_EVT     CMD(MOD_TWAI, 0x80) // flags u8|id u32|data 0..8

// I2S (std mode). Link bandwidth caps usable rates: ~92 KB/s at 921600 baud
// covers 16-bit/16 kHz mono (32 KB/s); 44.1 kHz stereo does NOT fit.
#define I2S_INIT        CMD(MOD_I2S, 0x01)  // dir u8 (bit0 tx, bit1 rx)|bclk i8|ws i8|dout i8|din i8|
                                            // rate u32|bits u8 (8/16/24/32)|stereo u8
#define I2S_WRITE       CMD(MOD_I2S, 0x02)  // pcm.. -> written u16 (blocking into DMA)
#define I2S_READ        CMD(MOD_I2S, 0x03)  // n u16 -> pcm.. (blocking from DMA)
#define I2S_DEINIT      CMD(MOD_I2S, 0x04)

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
// ESPNOW_INIT brings the Wi-Fi driver up lazily (STA) if it's still off.
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

// ETH (compile-time opt-in: BRIDGE_ENABLE_ETH). Once link+IP are up the
// existing NET sockets route over Ethernet automatically (unified Network).
// phy u8 is a STABLE wire id (the core's eth_phy_type_t values shift with
// chip config): 0 generic, 1 LAN8720, 2 TLK110/IP101, 3 RTL8201, 4 DP83848,
// 5 KSZ8041, 6 KSZ8081 (RMII) | 16 DM9051, 17 W5500, 18 KSZ8851 (SPI).
// Board presets live in Python (espbridge.eth.PRESETS).
#define ETH_BEGIN_RMII  CMD(MOD_ETH, 0x01)  // phy u8|addr i8|mdc i8|mdio i8|power i8|clk u8 (classic ESP32 only)
#define ETH_BEGIN_SPI   CMD(MOD_ETH, 0x02)  // phy u8|addr i8|cs i8|irq i8|rst i8|sck i8|miso i8|mosi i8|freq_mhz u8
#define ETH_STOP        CMD(MOD_ETH, 0x03)
#define ETH_STATUS      CMD(MOD_ETH, 0x04)  // -> link u8|ip[4]|gw[4]|mask[4]|mac[6]
#define ETH_STATE_EVT   CMD(MOD_ETH, 0x80)  // event u8 (1 connected,2 got_ip,3 disconnected)|ip[4]

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

// FS — LittleFS (internal flash) + SD card. All path ops take the filesystem
// id first: 0 = littlefs, 1 = sd_spi, 2 = sd_mmc. Paths are absolute ("/x").
#define FS_MOUNT        CMD(MOD_FS, 0x01)  // fs u8|[sd_spi: cs u8|sck i8|miso i8|mosi i8|freq_mhz u8]
                                           // -> total_kb u32|used_kb u32
#define FS_UMOUNT       CMD(MOD_FS, 0x02)  // fs u8
#define FS_OPEN         CMD(MOD_FS, 0x03)  // fs u8|mode u8 (0 r,1 w,2 a)|path str -> fd u8|size u32
#define FS_READ         CMD(MOD_FS, 0x04)  // fd u8|n u16 -> data (short = EOF)
#define FS_WRITE        CMD(MOD_FS, 0x05)  // fd u8|data.. -> written u16
#define FS_SEEK         CMD(MOD_FS, 0x06)  // fd u8|pos u32
#define FS_CLOSE        CMD(MOD_FS, 0x07)  // fd u8
#define FS_LIST         CMD(MOD_FS, 0x08)  // fs u8|path str; entries stream as FS_LIST_EVT, reply count u16 = done
#define FS_STAT         CMD(MOD_FS, 0x09)  // fs u8|path str -> size u32|isdir u8|mtime u32
#define FS_REMOVE       CMD(MOD_FS, 0x0A)  // fs u8|path str (file or empty dir)
#define FS_RENAME       CMD(MOD_FS, 0x0B)  // fs u8|from_len u8|from|to str
#define FS_MKDIR        CMD(MOD_FS, 0x0C)  // fs u8|path str
#define FS_DF           CMD(MOD_FS, 0x0D)  // fs u8 -> total_kb u32|used_kb u32
#define FS_LIST_EVT     CMD(MOD_FS, 0x80)  // isdir u8|size u32|name..

// NVS — persistent key/value bytes in the "user" namespace (separate from the
// bridge's own settings). Typed encode/decode is the host's job. Key <= 15 chars.
#define NVS_SET         CMD(MOD_NVS, 0x01) // klen u8|key|data..
#define NVS_GET         CMD(MOD_NVS, 0x02) // key str -> data (ST_NOT_FOUND if absent)
#define NVS_DEL         CMD(MOD_NVS, 0x03) // key str
#define NVS_KEYS        CMD(MOD_NVS, 0x04) // -> n u8|{klen u8|key}*n
#define NVS_CLEAR       CMD(MOD_NVS, 0x05)

// OTA — reflash the firmware over any transport (USB serial or BLE).
// Requires a partition table with two app slots (e.g. "Minimal SPIFFS";
// the default "Huge APP" has no OTA slot and OTA_BEGIN replies ST_UNSUPPORTED).
#define OTA_BEGIN       CMD(MOD_OTA, 0x01) // size u32 (0xFFFFFFFF = unknown)
#define OTA_WRITE       CMD(MOD_OTA, 0x02) // data.. -> total_written u32
#define OTA_END         CMD(MOD_OTA, 0x03) // commit u8; commit=1 -> reply, flush, reboot into new fw
#define OTA_ABORT       CMD(MOD_OTA, 0x04)

#define OTA_CHUNK 1024  // recommended bytes per OTA_WRITE

// CAM (compile-time opt-in: BRIDGE_ENABLE_CAM; esp32/s2/s3 + PSRAM).
// Pin maps and property ids live in Python (espbridge.camera board presets).
#define CAM_INIT        CMD(MOD_CAM, 0x01) // pwdn i8|reset i8|xclk i8|siod i8|sioc i8|d7..d0 i8[8]|
                                           // vsync i8|href i8|pclk i8|xclk_mhz u8|framesize u8|jpeg_q u8|fb_count u8
#define CAM_CAPTURE     CMD(MOD_CAM, 0x02) // -> len u32|w u16|h u16|format u8 (frame held for CAM_READ)
#define CAM_READ        CMD(MOD_CAM, 0x03) // offset u32|n u16 -> data
#define CAM_RELEASE     CMD(MOD_CAM, 0x04) // return the held frame buffer
#define CAM_SET         CMD(MOD_CAM, 0x05) // prop u8|val i32 (sensor_t setter id; ids defined host-side)
#define CAM_DEINIT      CMD(MOD_CAM, 0x06)
