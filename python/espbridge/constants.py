"""python-esp-bridge — shared protocol contract.

MUST stay in sync with src/espbridge/commands.h.
"""
from __future__ import annotations

import enum

PROTOCOL_VERSION = 1

# Frame (logical, pre-COBS):
#   flags u8 | seq u8 | cmd u16 BE | payload .. | crc16 BE
# COBS-encoded on the wire, frames delimited by 0x00.
FLAG_EVENT = 0x01
FLAG_ERROR = 0x02
FLAG_MORE = 0x04

MAX_PAYLOAD = 2048
NET_CHUNK = 512
UART_CHUNK = 256


class Status(enum.IntEnum):
    OK = 0x00
    UNKNOWN_CMD = 0x01
    BAD_ARGS = 0x02
    UNSUPPORTED = 0x03
    BUSY = 0x04
    TIMEOUT = 0x05
    NO_MEM = 0x06
    BAD_PIN = 0x07
    NOT_INIT = 0x08
    IO = 0x09
    WIFI = 0x0A
    SOCKET = 0x0B
    CRC = 0x0C
    DENIED = 0x0D  # wireless link not authenticated (see SYS_AUTH)
    NOT_FOUND = 0x0E  # no such key/path/peripheral instance


class Cap(enum.IntFlag):
    WIFI = 1 << 0
    BLE = 1 << 1
    BT_CLASSIC = 1 << 2
    DAC = 1 << 3
    TOUCH = 1 << 4
    HALL = 1 << 5
    PSRAM = 1 << 6
    NATIVE_USB = 1 << 7
    BLE_FW = 1 << 8
    BLE_LINK = 1 << 9  # bridge reachable over the BLE transport
    ESPNOW = 1 << 10  # ESP-NOW connectionless messaging
    RMT = 1 << 11  # generic pulse-train play/capture
    ONEWIRE = 1 << 12  # 1-Wire bit-timing primitives
    TWAI = 1 << 13  # CAN bus (TWAI controller)
    I2S = 1 << 14  # I2S audio in/out
    FS = 1 << 15  # filesystem access (LittleFS/SD)
    NVS = 1 << 16  # persistent key/value store
    OTA = 1 << 17  # firmware update over the link
    ETH = 1 << 18  # Ethernet (compile-time opt-in)
    CAM = 1 << 19  # camera (compile-time opt-in, needs PSRAM)
    MCPWM = 1 << 20  # complementary PWM pair with deadtime
    SLEEP = 1 << 21  # deep/light sleep (IRAM-gated on classic+BLE)


class ChipModel(enum.IntEnum):
    UNKNOWN = 0
    ESP32 = 1
    ESP32S2 = 2
    ESP32S3 = 3
    ESP32C3 = 4
    ESP32C6 = 5
    ESP32H2 = 6
    NRF52840 = 7  # Nordic nRF52840 (Seeed XIAO / Adafruit Bluefruit core)


def _cmd(mod: int, op: int) -> int:
    return (mod << 8) | op


MOD_SYS = 0x00
MOD_GPIO = 0x10
MOD_ADC = 0x20
MOD_DAC = 0x21
MOD_TOUCH = 0x22
MOD_PWM = 0x30
MOD_RMT = 0x31
MOD_MCPWM = 0x33
MOD_I2C = 0x40
MOD_SPI = 0x41
MOD_UART = 0x42
MOD_ONEWIRE = 0x43
MOD_TWAI = 0x44
MOD_I2S = 0x45
MOD_WIFI = 0x50
MOD_NET = 0x51
MOD_ESPNOW = 0x52
MOD_ETH = 0x53
MOD_BLE = 0x60
MOD_FS = 0x70
MOD_NVS = 0x71
MOD_OTA = 0x72
MOD_CAM = 0x73
MOD_WATCH = 0x74

# SYS
SYS_PING = _cmd(MOD_SYS, 0x01)
SYS_INFO = _cmd(MOD_SYS, 0x02)
SYS_SET_BAUD = _cmd(MOD_SYS, 0x03)
SYS_RESET = _cmd(MOD_SYS, 0x04)
SYS_FREE_HEAP = _cmd(MOD_SYS, 0x05)
SYS_SET_NAME = _cmd(MOD_SYS, 0x06)
SYS_AUTH = _cmd(MOD_SYS, 0x07)
SYS_SLEEP = _cmd(MOD_SYS, 0x08)
SYS_WAKE_CAUSE = _cmd(MOD_SYS, 0x09)
SYS_CPU_FREQ = _cmd(MOD_SYS, 0x0A)
SYS_LINK_POWER = _cmd(MOD_SYS, 0x0B)
SYS_READY = _cmd(MOD_SYS, 0x80)
SYS_LOG = _cmd(MOD_SYS, 0x81)

BRIDGE_NAME_MAX = 32

# GPIO
GPIO_SET_MODE = _cmd(MOD_GPIO, 0x01)
GPIO_WRITE = _cmd(MOD_GPIO, 0x02)
GPIO_READ = _cmd(MOD_GPIO, 0x03)
GPIO_WRITE_MASK = _cmd(MOD_GPIO, 0x04)
GPIO_READ_ALL = _cmd(MOD_GPIO, 0x05)
GPIO_WATCH = _cmd(MOD_GPIO, 0x06)
GPIO_UNWATCH = _cmd(MOD_GPIO, 0x07)
GPIO_STATUS = _cmd(MOD_GPIO, 0x08)
GPIO_DUMP = _cmd(MOD_GPIO, 0x09)
GPIO_EDGE_EVT = _cmd(MOD_GPIO, 0x80)

# ADC / DAC / TOUCH
ADC_CONFIG = _cmd(MOD_ADC, 0x01)
ADC_READ = _cmd(MOD_ADC, 0x02)
ADC_READ_MV = _cmd(MOD_ADC, 0x03)
DAC_WRITE = _cmd(MOD_DAC, 0x01)
DAC_COSINE = _cmd(MOD_DAC, 0x02)
DAC_COS_STOP = _cmd(MOD_DAC, 0x03)
DAC_DISABLE = _cmd(MOD_DAC, 0x04)
TOUCH_READ = _cmd(MOD_TOUCH, 0x01)

# PWM
PWM_ATTACH = _cmd(MOD_PWM, 0x01)
PWM_WRITE = _cmd(MOD_PWM, 0x02)
PWM_DETACH = _cmd(MOD_PWM, 0x03)
PWM_TONE = _cmd(MOD_PWM, 0x04)

# RMT (symbol = u16 BE: level<<15 | duration ticks)
RMT_INIT = _cmd(MOD_RMT, 0x01)
RMT_DEINIT = _cmd(MOD_RMT, 0x02)
RMT_TX = _cmd(MOD_RMT, 0x03)
RMT_TX_BYTES = _cmd(MOD_RMT, 0x04)
RMT_TX_LOOP = _cmd(MOD_RMT, 0x05)
RMT_TX_STOP = _cmd(MOD_RMT, 0x06)
RMT_RECV = _cmd(MOD_RMT, 0x07)
RMT_CARRIER = _cmd(MOD_RMT, 0x08)

RMT_MAX_RX_SYMS = 1020

# MCPWM
MCPWM_INIT = _cmd(MOD_MCPWM, 0x01)
MCPWM_DUTY = _cmd(MOD_MCPWM, 0x02)
MCPWM_STOP = _cmd(MOD_MCPWM, 0x03)

# I2C
I2C_INIT = _cmd(MOD_I2C, 0x01)
I2C_SCAN = _cmd(MOD_I2C, 0x02)
I2C_WRITE = _cmd(MOD_I2C, 0x03)
I2C_READ = _cmd(MOD_I2C, 0x04)
I2C_WRITE_READ = _cmd(MOD_I2C, 0x05)
I2C_DEINIT = _cmd(MOD_I2C, 0x06)

# SPI
SPI_INIT = _cmd(MOD_SPI, 0x01)
SPI_TRANSFER = _cmd(MOD_SPI, 0x02)
SPI_DEINIT = _cmd(MOD_SPI, 0x03)

# UART
UART_INIT = _cmd(MOD_UART, 0x01)
UART_WRITE = _cmd(MOD_UART, 0x02)
UART_DEINIT = _cmd(MOD_UART, 0x03)
UART_RX_EVT = _cmd(MOD_UART, 0x80)

# ONEWIRE
OW_RESET = _cmd(MOD_ONEWIRE, 0x01)
OW_WRITE = _cmd(MOD_ONEWIRE, 0x02)
OW_READ = _cmd(MOD_ONEWIRE, 0x03)
OW_TRIPLET = _cmd(MOD_ONEWIRE, 0x04)

# TWAI (CAN)
TWAI_INIT = _cmd(MOD_TWAI, 0x01)
TWAI_SEND = _cmd(MOD_TWAI, 0x02)
TWAI_STATUS = _cmd(MOD_TWAI, 0x03)
TWAI_RECOVER = _cmd(MOD_TWAI, 0x04)
TWAI_DEINIT = _cmd(MOD_TWAI, 0x05)
TWAI_RX_EVT = _cmd(MOD_TWAI, 0x80)

# I2S
I2S_INIT = _cmd(MOD_I2S, 0x01)
I2S_WRITE = _cmd(MOD_I2S, 0x02)
I2S_READ = _cmd(MOD_I2S, 0x03)
I2S_DEINIT = _cmd(MOD_I2S, 0x04)

# WIFI
WIFI_SCAN = _cmd(MOD_WIFI, 0x01)
WIFI_CONNECT = _cmd(MOD_WIFI, 0x02)
WIFI_DISCONNECT = _cmd(MOD_WIFI, 0x03)
WIFI_STATUS = _cmd(MOD_WIFI, 0x04)
WIFI_AP_START = _cmd(MOD_WIFI, 0x05)
WIFI_AP_STOP = _cmd(MOD_WIFI, 0x06)
WIFI_HOSTNAME = _cmd(MOD_WIFI, 0x07)
WIFI_STATE_EVT = _cmd(MOD_WIFI, 0x80)
WIFI_SCAN_RES = _cmd(MOD_WIFI, 0x81)
WIFI_SCAN_DONE = _cmd(MOD_WIFI, 0x82)

# NET
NET_TCP_CONNECT = _cmd(MOD_NET, 0x01)
NET_TCP_LISTEN = _cmd(MOD_NET, 0x02)
NET_UDP_OPEN = _cmd(MOD_NET, 0x03)
NET_SEND = _cmd(MOD_NET, 0x04)
NET_SEND_TO = _cmd(MOD_NET, 0x05)
NET_CLOSE = _cmd(MOD_NET, 0x06)
NET_WINDOW_ACK = _cmd(MOD_NET, 0x07)
NET_DATA_EVT = _cmd(MOD_NET, 0x80)
NET_ACCEPT_EVT = _cmd(MOD_NET, 0x81)
NET_CLOSED_EVT = _cmd(MOD_NET, 0x82)
NET_UDP_EVT = _cmd(MOD_NET, 0x83)

# ESP-NOW
ESPNOW_INIT = _cmd(MOD_ESPNOW, 0x01)
ESPNOW_DEINIT = _cmd(MOD_ESPNOW, 0x02)
ESPNOW_SET_PMK = _cmd(MOD_ESPNOW, 0x03)
ESPNOW_ADD_PEER = _cmd(MOD_ESPNOW, 0x04)
ESPNOW_DEL_PEER = _cmd(MOD_ESPNOW, 0x05)
ESPNOW_SEND = _cmd(MOD_ESPNOW, 0x06)
ESPNOW_POWER_SAVE = _cmd(MOD_ESPNOW, 0x07)
ESPNOW_RX_EVT = _cmd(MOD_ESPNOW, 0x80)
ESPNOW_SEND_EVT = _cmd(MOD_ESPNOW, 0x81)

ESPNOW_MAX_DATA = 250  # ESP_NOW_MAX_DATA_LEN

# ETH
ETH_BEGIN_RMII = _cmd(MOD_ETH, 0x01)
ETH_BEGIN_SPI = _cmd(MOD_ETH, 0x02)
ETH_STOP = _cmd(MOD_ETH, 0x03)
ETH_STATUS = _cmd(MOD_ETH, 0x04)
ETH_STATE_EVT = _cmd(MOD_ETH, 0x80)

# BLE
BLE_SCAN_START = _cmd(MOD_BLE, 0x01)
BLE_SCAN_STOP = _cmd(MOD_BLE, 0x02)
BLE_ADV_START = _cmd(MOD_BLE, 0x03)
BLE_ADV_STOP = _cmd(MOD_BLE, 0x04)
BLE_GATTS_DEF = _cmd(MOD_BLE, 0x05)
BLE_GATTS_SET = _cmd(MOD_BLE, 0x06)
BLE_GATTS_NTFY = _cmd(MOD_BLE, 0x07)
BLE_GATTC_CONN = _cmd(MOD_BLE, 0x08)
BLE_GATTC_DISC = _cmd(MOD_BLE, 0x09)
BLE_GATTC_READ = _cmd(MOD_BLE, 0x0A)
BLE_GATTC_WRITE = _cmd(MOD_BLE, 0x0B)
BLE_GATTC_SUB = _cmd(MOD_BLE, 0x0C)
BLE_ADV_EVT = _cmd(MOD_BLE, 0x80)
BLE_GATTS_WR_EVT = _cmd(MOD_BLE, 0x81)
BLE_GATTS_CONN_EVT = _cmd(MOD_BLE, 0x82)
BLE_GATTC_NTFY_EVT = _cmd(MOD_BLE, 0x83)
BLE_GATTC_DISC_EVT = _cmd(MOD_BLE, 0x84)

GATT_PROP_READ = 0x01
GATT_PROP_WRITE = 0x02
GATT_PROP_NOTIFY = 0x04
GATT_PROP_WRITE_NR = 0x08

# FS (fs id: 0 littlefs, 1 sd_spi, 2 sd_mmc)
FS_MOUNT = _cmd(MOD_FS, 0x01)
FS_UMOUNT = _cmd(MOD_FS, 0x02)
FS_OPEN = _cmd(MOD_FS, 0x03)
FS_READ = _cmd(MOD_FS, 0x04)
FS_WRITE = _cmd(MOD_FS, 0x05)
FS_SEEK = _cmd(MOD_FS, 0x06)
FS_CLOSE = _cmd(MOD_FS, 0x07)
FS_LIST = _cmd(MOD_FS, 0x08)
FS_STAT = _cmd(MOD_FS, 0x09)
FS_REMOVE = _cmd(MOD_FS, 0x0A)
FS_RENAME = _cmd(MOD_FS, 0x0B)
FS_MKDIR = _cmd(MOD_FS, 0x0C)
FS_DF = _cmd(MOD_FS, 0x0D)
FS_LIST_EVT = _cmd(MOD_FS, 0x80)

# NVS
NVS_SET = _cmd(MOD_NVS, 0x01)
NVS_GET = _cmd(MOD_NVS, 0x02)
NVS_DEL = _cmd(MOD_NVS, 0x03)
NVS_KEYS = _cmd(MOD_NVS, 0x04)
NVS_CLEAR = _cmd(MOD_NVS, 0x05)

# OTA
OTA_BEGIN = _cmd(MOD_OTA, 0x01)
OTA_WRITE = _cmd(MOD_OTA, 0x02)
OTA_END = _cmd(MOD_OTA, 0x03)
OTA_ABORT = _cmd(MOD_OTA, 0x04)

# Use the full payload for each OTA chunk (previously 1024 bytes), which halves
# the number of round-trips per MB. The firmware's OTA_WRITE handler passes
# whatever arrives directly to esp_ota_write, so the only ceiling is MAX_PAYLOAD;
# subtracting 8 leaves room for the frame header and CRC.
OTA_CHUNK = MAX_PAYLOAD - 8

# CAM
CAM_INIT = _cmd(MOD_CAM, 0x01)
CAM_CAPTURE = _cmd(MOD_CAM, 0x02)
CAM_READ = _cmd(MOD_CAM, 0x03)
CAM_RELEASE = _cmd(MOD_CAM, 0x04)
CAM_SET = _cmd(MOD_CAM, 0x05)
CAM_DEINIT = _cmd(MOD_CAM, 0x06)

# WATCH — polled, user-definable on-device event rules
WATCH_ADD = _cmd(MOD_WATCH, 0x01)
WATCH_REMOVE = _cmd(MOD_WATCH, 0x02)
WATCH_CLEAR = _cmd(MOD_WATCH, 0x03)
WATCH_LIST = _cmd(MOD_WATCH, 0x04)
WATCH_EVT = _cmd(MOD_WATCH, 0x80)

# Commands that must NOT be auto-retried after a response timeout. If the
# original request reached the firmware but only the reply was lost, re-sending
# one of these would double-send data, advance a file/bus cursor, or re-trigger
# a one-shot side effect. Everything else on the bridge sets a level or state
# and is safely idempotent. When in doubt a command is included here — see
# Bridge.request(retries=) for the retry logic.
NON_IDEMPOTENT = frozenset({
    SYS_SET_BAUD, SYS_RESET, SYS_SLEEP,
    UART_WRITE,
    OW_WRITE, OW_READ, OW_TRIPLET,   # 1-Wire transfers consume bus bits
    RMT_TX, RMT_TX_BYTES,            # would replay the pulse train
    TWAI_SEND, I2S_WRITE,
    NET_SEND, NET_SEND_TO, ESPNOW_SEND,
    FS_OPEN, FS_READ, FS_WRITE,      # fd allocation / file-position cursor
    OTA_BEGIN, OTA_WRITE, OTA_END,   # sequential update state machine
    BLE_GATTC_WRITE,                 # writes to a foreign BLE device
    CAM_CAPTURE,                     # grabs/replaces the held frame buffer
})

# Reverse lookup table used in error messages and debug logs. A name is included
# only when its prefix matches the module that owns its value — this filters out
# same-prefix scalars like UART_CHUNK or ESPNOW_MAX_DATA, whose numeric values
# don't correspond to any command code.
_MOD_PREFIX = {
    MOD_SYS: "SYS_", MOD_GPIO: "GPIO_", MOD_ADC: "ADC_", MOD_DAC: "DAC_",
    MOD_TOUCH: "TOUCH_", MOD_PWM: "PWM_", MOD_RMT: "RMT_", MOD_MCPWM: "MCPWM_",
    MOD_I2C: "I2C_", MOD_SPI: "SPI_", MOD_UART: "UART_", MOD_ONEWIRE: "OW_",
    MOD_TWAI: "TWAI_", MOD_I2S: "I2S_", MOD_WIFI: "WIFI_", MOD_NET: "NET_",
    MOD_ESPNOW: "ESPNOW_", MOD_ETH: "ETH_", MOD_BLE: "BLE_", MOD_FS: "FS_",
    MOD_NVS: "NVS_", MOD_OTA: "OTA_", MOD_CAM: "CAM_", MOD_WATCH: "WATCH_",
}

_CMD_NAMES = {
    v: n for n, v in list(globals().items())
    if type(v) is int and 0 <= v <= 0xFFFF
    and n.startswith(_MOD_PREFIX.get(v >> 8, "\0"))
}


def cmd_name(cmd: int) -> str:
    """Human name for a command code: 0x4003 -> 'I2C_WRITE (0x4003)'."""
    name = _CMD_NAMES.get(cmd)
    return f"{name} (0x{cmd:04X})" if name else f"0x{cmd:04X}"


# USB-UART bridge chips found on ESP32 dev boards: (vid, pid) -> chip
KNOWN_USB_IDS = {
    (0x10C4, 0xEA60): "cp210x",   # CP2102/CP2104 (most DevKitC)
    (0x1A86, 0x7523): "ch340",    # CH340
    (0x1A86, 0x55D4): "ch9102",   # CH9102 (CH340 successor)
    (0x303A, None): "native",     # Espressif native USB (S2/S3/C3/...)
}

# BLE link: Nordic-UART-style GATT service the firmware exposes as a transport.
# MUST stay in sync with firmware link_ble.cpp.
BLE_LINK_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
BLE_LINK_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # host -> board (write)
BLE_LINK_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # board -> host (notify)
DEFAULT_PASSWORD = "espbridge"  # firmware default; change via EspBridge.begin()

# Upgraded baud per bridge chip — optimistic targets, not guarantees:
# Bridge._upgrade_baud() probes the driver and ladders down to 921600 on
# failure, so a board that can't do the listed rate still ends up fast.
# cp210x: 1.5M verified on CP2102 (2M garbles, 3M driver-rejected).
# ch340: divider does 2M not 1.5M. ch9102: rated to 4M; 1.5M is safe.
UPGRADE_BAUD = {
    "cp210x": 1500000,
    "ch340": 2000000,
    "ch9102": 1500000,
    "native": None,  # USB CDC ignores baud
    None: 921600,
}
