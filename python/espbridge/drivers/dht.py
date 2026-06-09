"""DHT11/DHT22 temperature & humidity sensors over the RMT primitive.

    from espbridge.drivers.dht import DHT
    sensor = DHT(esp, pin=4, model=22)
    temp_c, humidity = sensor.read()
"""
from __future__ import annotations

import time


def decode(symbols: list[tuple[int, int]]) -> bytes:
    """Pure decoder: list of (level, duration-in-1-us-ticks) RMT symbols -> the sensor's 5 data bytes.

    A data bit is a ~50 us low followed by a high pulse: ~26 us high = 0, ~70 us high = 1.
    The capture also includes the start trigger and the 80/80 us acknowledgment response;
    taking the LAST 40 qualifying (low, high) pairs skips those preamble pulses.
    """
    highs = [dur for i, (level, dur) in enumerate(symbols)
             if level == 1 and i > 0 and symbols[i - 1][0] == 0
             and 10 <= dur <= 110]
    if len(highs) < 40:
        raise ValueError(f"short read: {len(highs)} bits")
    bits = highs[-40:]
    data = bytearray(5)
    for i, dur in enumerate(bits):
        if dur > 48:
            data[i // 8] |= 1 << (7 - i % 8)
    if (sum(data[:4]) & 0xFF) != data[4]:
        raise ValueError("checksum mismatch")
    return bytes(data)


def convert(data: bytes, model: int) -> tuple[float, float]:
    """Sensor bytes -> (temperature C, relative humidity %)."""
    if model == 11:
        return data[2] + data[3] / 10.0, data[0] + data[1] / 10.0
    temp = ((data[2] & 0x7F) << 8 | data[3]) / 10.0
    if data[2] & 0x80:
        temp = -temp
    return temp, (data[0] << 8 | data[1]) / 10.0


class DHT:
    def __init__(self, bridge, pin: int, model: int = 22):
        if model not in (11, 22):
            raise ValueError("model must be 11 or 22")
        self._rmt = bridge.rmt
        self._pin = pin
        self.model = model
        self._rmt.init_rx(pin, 1_000_000)
        self._last = 0.0

    def read(self, retries: int = 2) -> tuple[float, float]:
        """-> (temperature C, humidity %). Sensors need >=2 s between reads."""
        for attempt in range(retries + 1):
            wait = 2.0 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            # Start signal: pull the data line low (18 ms works for both
            # models), release, capture the 40-bit reply. Idle threshold
            # 150 us > the longest valid pulse (80 us response high).
            syms = self._rmt.recv(self._pin, idle_ticks=150, timeout_ms=100,
                                  max_symbols=200,
                                  trigger=(self._pin, 0, 18_000))
            self._last = time.monotonic()
            try:
                return convert(decode(syms), self.model)
            except ValueError:
                if attempt == retries:
                    raise

    def deinit(self) -> None:
        self._rmt.deinit(self._pin)
