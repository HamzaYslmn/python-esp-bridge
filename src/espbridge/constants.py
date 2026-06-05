"""python-esp-bridge — shared protocol contract.

MUST stay in sync with esp/src/espbridge/commands.h.
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


class ChipModel(enum.IntEnum):
    UNKNOWN = 0
    ESP32 = 1
    ESP32S2 = 2
    ESP32S3 = 3
    ESP32C3 = 4
    ESP32C6 = 5
    ESP32H2 = 6


def _cmd(mod: int, op: int) -> int:
    return (mod << 8) | op


MOD_SYS = 0x00
MOD_GPIO = 0x10
MOD_ADC = 0x20
MOD_DAC = 0x21
MOD_TOUCH = 0x22
MOD_PWM = 0x30
MOD_I2C = 0x40
MOD_SPI = 0x41
MOD_UART = 0x42
MOD_WIFI = 0x50
MOD_NET = 0x51
MOD_BLE = 0x60

# SYS
SYS_PING = _cmd(MOD_SYS, 0x01)
SYS_INFO = _cmd(MOD_SYS, 0x02)
SYS_SET_BAUD = _cmd(MOD_SYS, 0x03)
SYS_RESET = _cmd(MOD_SYS, 0x04)
SYS_FREE_HEAP = _cmd(MOD_SYS, 0x05)
SYS_SET_NAME = _cmd(MOD_SYS, 0x06)
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

# USB-UART bridge chips found on ESP32 dev boards: (vid, pid) -> chip
KNOWN_USB_IDS = {
    (0x10C4, 0xEA60): "cp210x",   # CP2102/CP2104 (most DevKitC)
    (0x1A86, 0x7523): "ch340",    # CH340
    (0x1A86, 0x55D4): "ch9102",   # CH9102 (CH340 successor)
    (0x303A, None): "native",     # Espressif native USB (S2/S3/C3/...)
}

# Safe upgraded baud per bridge chip (conservative defaults; CH340 can do 2M).
UPGRADE_BAUD = {
    "cp210x": 921600,
    "ch340": 921600,
    "ch9102": 921600,
    "native": None,  # USB CDC ignores baud
    None: 921600,
}
