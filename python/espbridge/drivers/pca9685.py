"""PCA9685 — 16-channel, 12-bit PWM / servo driver (I2C).

A "bring your own" driver: a plain class over the bridge's I2C primitive, no
firmware change (see docs/DRIVERS.md).

    from espbridge import Bridge

    with Bridge() as esp:
        pwm = esp.pca9685(address=0x40, freq=50)   # 50 Hz for servos
        pwm.set_servo(0, 90)                        # channel 0 to mid (1500us)
        pwm.set_servo(0, 0, min_us=600, max_us=2400)

        # LED dimming at a higher refresh rate:
        led = esp.pca9685(address=0x41, freq=1000)
        led.duty(3, 0.25)                           # channel 3 at 25%
        led.set_pwm(4, 0, 2048)                     # raw on/off counts
        led.off(5)                                  # channel 5 fully off

Each of the 16 outputs has a 12-bit ON count and a 12-bit OFF count: the output
goes high at ON and low at OFF within the 4096-step (0..4095) PWM period, giving
phase control as well as duty. Addresses are 0x40..0x7F, set by the A0..A5 strap
pins (0x40 with all straps low).

Datasheet (NXP PCA9685, Rev. 4):
https://cdn-shop.adafruit.com/datasheets/PCA9685.pdf
- MODE1 0x00, MODE2 0x01, PRE_SCALE 0xFE; LEDn at 0x06 + 4*n (ON_L/ON_H/OFF_L/
  OFF_H), ALL_LED at 0xFA (datasheet Table 4, "Register definitions").
- prescale = round(osc_clock / (4096 * update_rate)) - 1, osc = 25 MHz internal
  (datasheet 7.3.5, eq. 1). Default PRE_SCALE 0x1E -> 200 Hz.
- PRE_SCALE is writable only while the SLEEP bit (MODE1 bit4) is 1
  (datasheet 7.3.5). Restart needs >= 500 us after clearing SLEEP, then set
  RESTART (MODE1 bit7) (datasheet 7.3.1.1).
"""
from __future__ import annotations

from ..i2c import bind_i2c

import struct
import time

# --- Registers (datasheet Table 4) -----------------------------------------
_MODE1 = 0x00
_MODE2 = 0x01
_LED0_ON_L = 0x06        # channel n bytes live at 0x06 + 4*n
_ALL_LED_ON_L = 0xFA
_PRESCALE = 0xFE

# --- MODE1 bits (datasheet Table 5) ----------------------------------------
_RESTART = 0x80
_AI = 0x20               # register auto-increment (needed for multi-byte writes)
_SLEEP = 0x10            # low-power; oscillator off, PRE_SCALE writable

_OSC_CLOCK = 25_000_000  # internal oscillator, Hz (datasheet 7.3.5)
_FULL_OFF = 0x10         # OFF_H bit4: force output fully off (datasheet 7.3.3)


class PCA9685:
    def __init__(self, bridge, address: int = 0x40, *, bus: int = 0,
                 sda: int | None = None, scl: int | None = None,
                 freq: int = 50):
        if not 0x40 <= address <= 0x7F:
            raise ValueError(f"PCA9685 address {address:#04x} out of range "
                             f"(0x40-0x7F)")
        self._i2c, self._addr, self._bus = bind_i2c(bridge, address, bus=bus, sda=sda, scl=scl)
        self._freq = 0
        # Enable auto-increment and wake (clear SLEEP) so multi-byte LED writes
        # land in consecutive registers.
        self._i2c.write_reg(self._addr, _MODE1, _AI, self._bus)
        self.set_freq(freq)

    def set_freq(self, hz: float) -> None:
        """Set the PWM update rate (Hz) for all channels.

        PRE_SCALE only takes effect while asleep, so we sleep, write it, wake,
        wait for the oscillator, then RESTART (datasheet 7.3.5 / 7.3.1.1)."""
        if hz <= 0:
            raise ValueError(f"frequency must be > 0, got {hz}")
        # prescale = round(osc / (4096 * freq)) - 1, clamped to 3..255.
        prescale = round(_OSC_CLOCK / (4096 * hz)) - 1
        prescale = max(3, min(255, prescale))
        old = self._i2c.read_reg(self._addr, _MODE1, 1, self._bus)[0]
        sleep = (old & ~_RESTART) | _SLEEP
        self._i2c.write_reg(self._addr, _MODE1, sleep, self._bus)    # to sleep
        self._i2c.write_reg(self._addr, _PRESCALE, prescale, self._bus)
        self._i2c.write_reg(self._addr, _MODE1, old, self._bus)      # wake
        time.sleep(0.001)                          # >= 500 us
        self._i2c.write_reg(self._addr, _MODE1, old | _RESTART, self._bus)
        self._freq = hz

    @property
    def freq(self) -> float:
        """The currently configured PWM update rate (Hz)."""
        return self._freq

    def set_pwm(self, channel: int, on: int, off: int) -> None:
        """Set raw 12-bit ON and OFF counts (0..4095) for one channel."""
        self._check(channel)
        if not 0 <= on <= 4095 or not 0 <= off <= 4095:
            raise ValueError("on/off counts must be 0..4095")
        reg = _LED0_ON_L + 4 * channel
        # Byte order ON_L, ON_H, OFF_L, OFF_H (datasheet Table 4).
        self._i2c.write(self._addr, bytes([reg]) + struct.pack("<HH", on, off), self._bus)

    def duty(self, channel: int, fraction: float) -> None:
        """Set duty cycle as a fraction 0.0..1.0 (output on from count 0)."""
        if not 0.0 <= fraction <= 1.0:
            raise ValueError("fraction must be 0.0..1.0")
        if fraction <= 0.0:
            self.off(channel)
            return
        off = min(4095, round(fraction * 4096))
        self.set_pwm(channel, 0, off)

    def set_servo(self, channel: int, angle: float,
                  min_us: int = 500, max_us: int = 2500) -> None:
        """Drive a hobby servo to `angle` degrees (0..180).

        Pulse width = min_us + angle/180 * (max_us - min_us); the OFF count is
        that width scaled to the current PWM period."""
        if not 0 <= angle <= 180:
            raise ValueError(f"angle must be 0..180, got {angle}")
        pulse_us = min_us + angle / 180 * (max_us - min_us)
        count = round(pulse_us * self._freq * 4096 / 1_000_000)
        self.set_pwm(channel, 0, count)

    def off(self, channel: int) -> None:
        """Turn a channel fully off (OFF_H full-off bit, datasheet 7.3.3)."""
        self._check(channel)
        reg = _LED0_ON_L + 4 * channel
        self._i2c.write(self._addr, bytes([reg, 0x00, 0x00, 0x00, _FULL_OFF]), self._bus)

    @staticmethod
    def _check(channel: int) -> None:
        if not 0 <= channel <= 15:
            raise ValueError(f"PCA9685 channel must be 0..15, got {channel}")
