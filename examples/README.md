# Examples

Flash the firmware first ([`../firmware/README.md`](../firmware/README.md)), plug the
ESP32 in via USB (or pair over Bluetooth — see `wireless/`), then run any
example with [uv](https://docs.astral.sh/uv/) — the local `espbridge` package
from `../src` is installed automatically (editable, so library changes apply
immediately):

```sh
cd examples
uv run basics/blink.py
uv run displays/oled_ssd1306.py
uv run wireless/ble_blink.py      # no USB needed
```

Without uv: `pip install -e "../src[oled,ble]"` once, then `python basics/blink.py`.

## basics/ — core peripherals over USB

| example | shows |
|---|---|
| `blink.py` | GPIO output, ping latency |
| `button_interrupt.py` | edge interrupts with debounce → Python callback |
| `adc_dac.py` | analog read (raw + mV) and true DAC output + cosine generator |
| `pwm_servo.py` | LEDC PWM fade + hobby servo sweep |
| `i2c_scan.py` | I2C bus scan |
| `spi_transfer.py` | full-duplex SPI (flash JEDEC ID) |
| `rtos_concurrency.py` | FreeRTOS task split: fast lane stays ~ms while radio blocks |

## devices/ — sensors, LEDs, motors, buses (pure-Python drivers)

| example | shows |
|---|---|
| `neopixel_rainbow.py` | WS2812/NeoPixel strip animation over the RMT primitive |
| `dht_read.py` | DHT22/DHT11 temperature + humidity |
| `ds18b20_temp.py` | DS18B20 1-Wire thermometers — several probes on one pin |
| `hcsr04_ping.py` | HC-SR04 ultrasonic distance |
| `ir_remote.py` | receive NEC remote codes and replay them out an IR LED |
| `stepper_move.py` | A4988/DRV8825 stepper with trapezoidal ramps + free-run |
| `can_dump.py` | CAN bus monitor + periodic frame (TWAI, needs a transceiver) |
| `i2s_record.py` | record an I2S MEMS mic to a WAV file on the host |

## system/ — on-board storage, sleep, firmware updates

| example | shows |
|---|---|
| `nvs_counter.py` | persistent key/value storage in the ESP32's flash |
| `fs_logger.py` | LittleFS files: append a log on the board, read it back |
| `deep_sleep.py` | deep sleep + timer wake (see chip notes in the main README) |
| `ota_update.py` | **reflash the firmware over USB or Bluetooth** — no boot button |

## wireless/ — Bluetooth link, ESP-NOW, multiple boards, link speed

| example | shows |
|---|---|
| `ble_blink.py` | **GPIO over Bluetooth — no USB cable.** Default password `espbridge` |
| `ble_scan.py` | BLE advertisement scan (ESP32 as scanner, over USB) |
| `espnow_pair.py` | **two-board ESP-NOW chat, fully wireless** — Bluetooth to the board, ESP-NOW between boards, delivery ACKs |
| `espnow_broadcast.py` | ESP-NOW broadcast over a Bluetooth-connected bridge: one sender, any number of listeners, RSSI per packet |
| `multi_device.py` | several boards, addressed by persistent name |
| `benchmark.py` | latency + throughput, first over USB then over Bluetooth |

Bridges advertise as `espbridge_<mac>` (or `espbridge_<mac>_<name>` once you
`espbridge set-name`), so `espbridge scan --ble` finds and labels every board.

The Bluetooth password defaults to `espbridge`; change it by editing
`#define BRIDGE_PASSWORD` at the top of `../firmware/firmware.ino` and reflashing.

## network/ — Wi-Fi through the ESP32 radio

| example | shows |
|---|---|
| `wifi_scan.py` | Wi-Fi scan through the ESP32 radio |
| `tcp_through_bridge.py` | join Wi-Fi, HTTP + raw TCP through the ESP32 |
| `udp_through_bridge.py` | UDP datagrams through the ESP32 |

## displays/

| example | shows |
|---|---|
| `oled_ssd1306.py` | SSD1306/SH1106 OLED via `espbridge.oled.OLED` — auto-handles the common clones, PIL drawing |
| `ble_oled.py` | the same display driven **over Bluetooth** — no COM port at all |

## compat/ — ecosystem integrations (espbridge.compat)

One folder per ecosystem — existing code from these libraries runs unchanged:

| example | ecosystem |
|---|---|
| [`compat/gpiozero/led_button.py`](compat/gpiozero/) | **gpiozero** LED/Button via the espbridge pin factory |
| [`compat/adafruit/bme280.py`](compat/adafruit/) | **Adafruit CircuitPython** drivers via the blinka shim |
| [`compat/luma/oled.py`](compat/luma/) | **luma.oled** display library |
| [`compat/smbus/read_register.py`](compat/smbus/) | **smbus2**-style I2C register access |
| [`compat/rpi_gpio/blink.py`](compat/rpi_gpio/) | **RPi.GPIO**-style scripts, only the import changes |
