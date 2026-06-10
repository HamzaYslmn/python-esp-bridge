// python-esp-bridge — shared Wi-Fi radio ownership.
//
// The 2.4 GHz Wi-Fi driver has up to three independent users — STA, AP and
// ESP-NOW — and a real heap cost (margins in config.h). Modules do not peek
// at each other's state to decide when the radio may come up or go down:
// each user claims and releases its bit here, and radio.cpp alone applies
// the heap guards and powers the driver off when no user remains.
// All callers run on slow_task, so no locking is needed.
#pragma once
#include <Arduino.h>

#define RADIO_STA    0x01
#define RADIO_AP     0x02
#define RADIO_ESPNOW 0x04

// Claim `user`'s radio bit, heap-guarded: with an authed BLE central,
// returns false when the acquire's actual cost (full driver bring-up when
// the radio is off; a few KB for STA/AP/scan on a warm driver; nothing for
// ESP-NOW joining a warm driver) would starve the link — caller replies
// ST_NO_MEM. Wi-Fi + BLE + ESP-NOW all running at once is supported.
// user = 0 runs the guard without claiming a bit (Wi-Fi scan: the radio
// comes up but nothing holds it). Does not change the Wi-Fi mode — the
// caller raises the mode it needs.
bool radio_acquire(uint8_t user);

// Release `user`'s bit; powers the radio off (WIFI_MODE_NULL) to reclaim
// ~50 KB of driver heap once no user remains.
void radio_release(uint8_t user);

bool radio_held(uint8_t user);  // is this user's bit currently claimed?
bool radio_active();            // Wi-Fi driver up? (ADC2-conflict guard)
