#if defined(ARDUINO_ARCH_ESP32)
// Shared Wi-Fi radio ownership — see radio.h.
#include "espbridge/radio.h"
#include "espbridge/config.h"
#include "espbridge/link.h"
#include <WiFi.h>

static uint8_t users = 0;  // RADIO_* bits of everyone holding the radio up

// The IDF Wi-Fi driver keeps ~18 KB resident once it has been started, even
// after the radio is turned back off — restarts cost less than first starts,
// so the heap guard switches to the matching *_REUP_* margin.
static bool driver_resident = false;

bool radio_acquire(uint8_t user) {
  // Refuse an acquire that would starve a live BLE session (margins in
  // config.h) — a clean ST_NO_MEM beats the radio eating the heap out from
  // under Bluedroid mid-command. Three cost tiers, matched to what the
  // acquire actually allocates:
  //   driver down:  full bring-up (~52 KB first, ~33 KB restart)
  //   driver up, ESP-NOW: joins the running driver for free — no guard
  //   driver up, STA/AP/scan: ~4-8 KB (netif, DHCP, scan results)
  // USB-only sessions are never guarded. All three radios CAN run at once;
  // the tiers just keep the BLE link alive while they come up.
  uint32_t need;
  if (WiFi.getMode() == WIFI_MODE_NULL) {
    need = (user == RADIO_ESPNOW)
        ? (driver_resident ? ESPNOW_REUP_BLE_MIN_HEAP : ESPNOW_UP_BLE_MIN_HEAP)
        : (driver_resident ? WIFI_REUP_BLE_MIN_HEAP   : WIFI_UP_BLE_MIN_HEAP);
  } else {
    need = (user == RADIO_ESPNOW) ? 0 : RADIO_WARM_BLE_MIN_HEAP;
  }
  if (need && link_ble_authed() && ESP.getFreeHeap() < need) return false;
  driver_resident = true;  // every successful caller proceeds to raise the radio
  users |= user;
  return true;
}

void radio_release(uint8_t user) {
  users &= ~user;
  // Powering the radio off reclaims ~50 KB of driver heap. This matters most
  // over BLE: nothing resets the board between Bluetooth sessions, so a
  // driver left resident would pin the heap near the Bluedroid floor and
  // cripple every later BLE session.
  if (!users) WiFi.mode(WIFI_MODE_NULL);
}

bool radio_held(uint8_t user) { return (users & user) != 0; }
bool radio_active() { return WiFi.getMode() != WIFI_MODE_NULL; }
#endif  // ARDUINO_ARCH_ESP32
