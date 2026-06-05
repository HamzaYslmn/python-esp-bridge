# python-esp-bridge

Control every ESP32 peripheral from Python over USB serial — GPIO, PWM, ADC,
DAC, touch, I2C, SPI, UART, Wi-Fi (with TCP/UDP sockets through the ESP32
radio) and BLE. Flash the bridge firmware once, then it's all Python.

```python
from espbridge import Bridge

with Bridge() as esp:                      # auto-detects the USB port
    esp.gpio.mode(2, "output")
    esp.gpio.write(2, 1)
    print(esp.adc.read_mv(34), "mV")
    esp.i2c.init(sda=21, scl=22)
    print(esp.i2c.scan())
    esp.wifi.connect("ssid", "password")
    status, body = esp.net.http_get("http://example.com/")
```

- Firmware (flash once with Arduino IDE) and full docs:
  **<https://github.com/HamzaYslmn/python-esp-bridge>**
- Works on Raspberry Pi OS, Linux, Windows, macOS (Python ≥ 3.10, pyserial).
- `espbridge` CLI: connection info; `espbridge ports`: list candidate ports.
