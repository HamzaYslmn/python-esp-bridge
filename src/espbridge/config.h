// python-esp-bridge — per-chip configuration & capability flags.
// Requires arduino-esp32 core 3.x.
#pragma once
#include <Arduino.h>
#include "commands.h"

// Set to 0 to build without BLE (smaller flash, more heap).
#ifndef BRIDGE_ENABLE_BLE
#define BRIDGE_ENABLE_BLE 1
#endif

#define BRIDGE_NAME "esp-bridge"

#if defined(CONFIG_IDF_TARGET_ESP32)
  #define BRIDGE_CHIP        CHIP_ESP32
  #define BRIDGE_HAS_DAC     1
  #define BRIDGE_HAS_TOUCH   1
  #define BRIDGE_HAS_BT_CLASSIC 1
  #define BRIDGE_HAS_BLE     1
  #define BRIDGE_HAS_ESPNOW  1
  #define BRIDGE_SPI_HOST0   VSPI
  #define BRIDGE_SPI_HOST1   HSPI
#elif defined(CONFIG_IDF_TARGET_ESP32S2)
  #define BRIDGE_CHIP        CHIP_ESP32S2
  #define BRIDGE_HAS_DAC     1
  #define BRIDGE_HAS_TOUCH   1
  #define BRIDGE_HAS_BT_CLASSIC 0
  #define BRIDGE_HAS_BLE     0
  #define BRIDGE_HAS_ESPNOW  1
  #define BRIDGE_SPI_HOST0   FSPI
  #define BRIDGE_SPI_HOST1   HSPI
#elif defined(CONFIG_IDF_TARGET_ESP32S3)
  #define BRIDGE_CHIP        CHIP_ESP32S3
  #define BRIDGE_HAS_DAC     0
  #define BRIDGE_HAS_TOUCH   1
  #define BRIDGE_HAS_BT_CLASSIC 0
  #define BRIDGE_HAS_BLE     1
  #define BRIDGE_HAS_ESPNOW  1
  #define BRIDGE_SPI_HOST0   FSPI
  #define BRIDGE_SPI_HOST1   HSPI
#elif defined(CONFIG_IDF_TARGET_ESP32C3)
  #define BRIDGE_CHIP        CHIP_ESP32C3
  #define BRIDGE_HAS_DAC     0
  #define BRIDGE_HAS_TOUCH   0
  #define BRIDGE_HAS_BT_CLASSIC 0
  #define BRIDGE_HAS_BLE     1
  #define BRIDGE_HAS_ESPNOW  1
  #define BRIDGE_SPI_HOST0   FSPI
  #define BRIDGE_SPI_HOST1   FSPI
#elif defined(CONFIG_IDF_TARGET_ESP32C6)
  #define BRIDGE_CHIP        CHIP_ESP32C6
  #define BRIDGE_HAS_DAC     0
  #define BRIDGE_HAS_TOUCH   0
  #define BRIDGE_HAS_BT_CLASSIC 0
  #define BRIDGE_HAS_BLE     1
  #define BRIDGE_HAS_ESPNOW  1
  #define BRIDGE_SPI_HOST0   FSPI
  #define BRIDGE_SPI_HOST1   FSPI
#else
  #define BRIDGE_CHIP        CHIP_UNKNOWN
  #define BRIDGE_HAS_DAC     0
  #define BRIDGE_HAS_TOUCH   0
  #define BRIDGE_HAS_BT_CLASSIC 0
  #define BRIDGE_HAS_BLE     0
  #define BRIDGE_HAS_ESPNOW  0
  #define BRIDGE_SPI_HOST0   FSPI
  #define BRIDGE_SPI_HOST1   FSPI
#endif

// ---- v0.3.0 modules ----------------------------------------------------------
// Generic primitives present on every supported chip; SOC_* macros (soc_caps.h
// via Arduino.h) gate the chip-dependent peripherals.
#define BRIDGE_HAS_RMT     1   // pulse-train play/capture
#define BRIDGE_HAS_ONEWIRE 1   // bit-banged 1-Wire timing primitives
#define BRIDGE_HAS_FS      1   // LittleFS always; SD depends on IRAM (below)
// Classic ESP32 with Wi-Fi + Bluedroid leaves ~zero spare IRAM; the SD-SPI
// and SDMMC drivers add IRAM-resident ISRs that overflow iram0_0_seg there.
// Set BRIDGE_ENABLE_BLE 0 (frees BT IRAM) to get SD support on classic.
#if defined(CONFIG_IDF_TARGET_ESP32) && BRIDGE_ENABLE_BLE
  #define BRIDGE_HAS_SD    0
#else
  #define BRIDGE_HAS_SD    1
#endif
#if BRIDGE_HAS_SD && defined(SOC_SDMMC_HOST_SUPPORTED)
  #define BRIDGE_HAS_SDMMC 1
#else
  #define BRIDGE_HAS_SDMMC 0
#endif
// The IDF sleep API (sleep_modes.c) is IRAM-resident — even deep sleep + wake
// cause overflow classic ESP32's last ~436 B of IRAM once Wi-Fi + Bluedroid
// are in (measured). Same trade as SD: build BRIDGE_ENABLE_BLE 0 (USB-only) to
// get sleep on classic. S2/S3/C3/C6 have it in every build.
#if defined(CONFIG_IDF_TARGET_ESP32) && BRIDGE_ENABLE_BLE
  #define BRIDGE_HAS_SLEEP 0
#else
  #define BRIDGE_HAS_SLEEP 1
#endif
#define BRIDGE_HAS_NVS     1   // key/value store
#define BRIDGE_HAS_OTA     1   // firmware update over the link
#ifdef SOC_TWAI_SUPPORTED
  #define BRIDGE_HAS_TWAI  1
#else
  #define BRIDGE_HAS_TWAI  0
#endif
#ifdef SOC_I2S_SUPPORTED
  #define BRIDGE_HAS_I2S   1
#else
  #define BRIDGE_HAS_I2S   0
#endif
#ifdef SOC_MCPWM_SUPPORTED  // esp32/s3/c6 — NOT s2/c3
  #define BRIDGE_HAS_MCPWM 1
#else
  #define BRIDGE_HAS_MCPWM 0
#endif

// Heavy opt-ins (default OFF; enable with -DBRIDGE_ENABLE_ETH=1). ETH needs a PHY chip
// (RMII on classic ESP32, or W5500/DM9051 over SPI on any chip); CAM needs an
// OV-series sensor + PSRAM and exists on esp32/s2/s3 only.
#ifndef BRIDGE_ENABLE_ETH
#define BRIDGE_ENABLE_ETH 0
#endif
#ifndef BRIDGE_ENABLE_CAM
#define BRIDGE_ENABLE_CAM 0
#endif
#if BRIDGE_ENABLE_ETH && defined(CONFIG_ETH_ENABLED)
  #define BRIDGE_ETH 1
#else
  #define BRIDGE_ETH 0
#endif
#if BRIDGE_ENABLE_CAM && (defined(CONFIG_IDF_TARGET_ESP32) || defined(CONFIG_IDF_TARGET_ESP32S2) || defined(CONFIG_IDF_TARGET_ESP32S3))
  #define BRIDGE_CAM 1
#else
  #define BRIDGE_CAM 0
#endif

// BLE additionally needs the Bluedroid host stack: arduino-esp32 3.3.x moved
// S3/C3/C6 to NimBLE, which link_ble.cpp / mod_ble.cpp don't speak (yet) —
// those chips build USB-only (ESP-NOW and everything else still works).
#if BRIDGE_ENABLE_BLE && BRIDGE_HAS_BLE && defined(SOC_BLE_SUPPORTED) && defined(CONFIG_BT_BLUEDROID_ENABLED)
  #define BRIDGE_BLE 1
#else
  #define BRIDGE_BLE 0
#endif

// Native USB CDC (S2/S3/C3/...): Serial is HWCDC, baud is meaningless.
#if defined(ARDUINO_USB_CDC_ON_BOOT) && ARDUINO_USB_CDC_ON_BOOT
  #define BRIDGE_NATIVE_USB 1
#else
  #define BRIDGE_NATIVE_USB 0
#endif

// Bridge tasks run on the app core; radio stacks own core 0 on dual-core chips.
#if CONFIG_FREERTOS_UNICORE
  #define BRIDGE_CORE 0
#else
  #define BRIDGE_CORE 1
#endif

// Frame buffers: logical frame = 4 hdr + MAX_PAYLOAD + 2 crc.
#define MAX_FRAME      (4 + MAX_PAYLOAD + 2)
// COBS overhead: 1 byte per started 254-byte block.
#define ENC_BUF_SIZE   (MAX_FRAME + (MAX_FRAME / 254) + 2)

#define SERIAL_RX_BUF  4096
#define SERIAL_TX_BUF  2048

#define NET_MAX_SOCKETS 8
#define NET_WINDOW      4096   // per-socket credit window (bytes)

#define BLE_MAX_CHARS   16
