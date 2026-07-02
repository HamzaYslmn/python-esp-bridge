# python-esp-bridge

Control every ESP32 peripheral from Python over USB serial — GPIO, PWM, ADC,
DAC, touch, I2C, SPI, UART, Wi-Fi (with TCP/UDP sockets through the ESP32
radio) and BLE. Flash the bridge firmware once, then it's all Python.

```python
from espbridge import Bridge

with Bridge() as esp:                      # Bluetooth first, then USB serial
    esp.gpio.mode(2, "output")
    esp.gpio.write(2, 1)
    print(esp.adc.read_mv(34), "mV")
    esp.i2c.init(sda=21, scl=22)
    print(esp.i2c.scan())
    esp.wifi.connect("ssid", "password")
    status, body = esp.net.http_get("http://example.com/")
```

- On-device rules react without the host in the loop
  (`esp.watch.add("adc", pin=34, above=3200, do=("pwm", 13, 0))`), and
  `esp.radio_off()` silences Wi-Fi + Bluetooth entirely for jitter-sensitive
  realtime work (frees ~110 KB heap, unlocks the ADC2 pins).
- Firmware (flash once with Arduino IDE) and full docs:
  **<https://github.com/HamzaYslmn/python-esp-bridge>**
- Works on Raspberry Pi OS, Linux, Windows, macOS (Python ≥ 3.11, pyserial).
- `espbridge` CLI: connection info; `espbridge ports`: list candidate ports.
