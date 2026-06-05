# Examples

Flash the firmware first ([`../esp/README.md`](../esp/README.md)), plug the
ESP32 in via USB, then run any example with [uv](https://docs.astral.sh/uv/) —
the local `espbridge` package from `../src` is installed automatically
(editable, so library changes apply immediately):

```sh
cd examples
uv run blink.py
uv run oled_ssd1306.py
uv run gpiozero/led_button.py
```

Without uv: `pip install -e ../src` once, then `python blink.py`.

## Native API (espbridge)

| example | shows |
|---|---|
| `blink.py` | GPIO output, ping latency |
| `button_interrupt.py` | edge interrupts with debounce → Python callback |
| `adc_dac.py` | analog read (raw + mV) and true DAC output + cosine generator |
| `pwm_servo.py` | LEDC PWM fade + hobby servo sweep |
| `i2c_scan.py` | I2C bus scan |
| `spi_transfer.py` | full-duplex SPI (flash JEDEC ID) |
| `oled_ssd1306.py` | SSD1306/SH1106 OLED via `espbridge.oled.OLED` — auto-handles the common clones, PIL drawing |
| `wifi_scan.py` | Wi-Fi scan through the ESP32 radio |
| `tcp_through_bridge.py` | join Wi-Fi, HTTP + raw TCP through the ESP32 |
| `udp_through_bridge.py` | UDP datagrams through the ESP32 |
| `ble_scan.py` | BLE advertisement scan |
| `rtos_concurrency.py` | FreeRTOS task split: fast lane stays ~ms while radio blocks |
| `multi_device.py` | several boards, addressed by persistent name |

## Ecosystem integrations (espbridge.compat)

One folder per ecosystem — existing code from these libraries runs unchanged:

| example | ecosystem |
|---|---|
| [`gpiozero/led_button.py`](gpiozero/) | **gpiozero** LED/Button via the espbridge pin factory |
| [`adafruit/bme280.py`](adafruit/) | **Adafruit CircuitPython** drivers via the blinka shim |
| [`luma/oled.py`](luma/) | **luma.oled** display library |
| [`smbus/read_register.py`](smbus/) | **smbus2**-style I2C register access |
| [`rpi_gpio/blink.py`](rpi_gpio/) | **RPi.GPIO**-style scripts, only the import changes |
