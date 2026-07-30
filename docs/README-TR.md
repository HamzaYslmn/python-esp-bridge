# python-esp-bridge

ESP32'yi USB **veya Bluetooth** üzerinden Raspberry Pi'ye ya da herhangi bir
bilgisayara bağlayın; GPIO, PWM, ADC, DAC, kapasitif dokunma, I2C, SPI, ek
UART'lar, RMT darbe dizileri (NeoPixel, IR, DHT, ultrasonik sensörler, step
motorlar), 1-Wire, CAN bus, I2S ses, dosyalar (LittleFS/SD), NVS depolama,
derin uyku, Wi-Fi (ESP32 radyosu üzerinden TCP/UDP soketleri dahil), Ethernet,
kamera, BLE ve ESP-NOW dahil **tüm** ESP32 çevre birimlerini Python'dan canlı
olarak kullanın. Firmware güncellemeleri de aynı bağlantı üzerinden yapılır.
Bridge firmware'ini **bir kez** yükleyin; sonrasında host tarafında her şey
Python'dır. Her proje için yeniden flash etmeye gerek yoktur.

Tasarım kuralı basit: firmware yalnızca en küçük donanım yapı taşlarını sunar;
cihaz protokolleri (WS2812 zamanlaması, NEC IR, DHT çözümleme, 1-Wire arama,
step motor rampaları) okunması, test edilmesi ve genişletilmesi kolay olduğu
için Python tarafında uygulanır.

```
┌────────────────┐  USB serial (≤2 Mbaud) veya BLE  ┌─────────────────────┐
│ Pi / PC        │ ───────────────────────────────► │ ESP32 (bridge fw)   │
│ Python:        │   binary protocol, COBS+CRC16    │ FreeRTOS tasks:     │
│  espbridge     │ ◄─────────────────────────────── │  tx / rx / network  │
└────────────────┘        yanıtlar + async olaylar  └─────────────────────┘
```

![Oled](img/oled.png)

## Hızlı başlangıç

*Raspberry Pi OS, Linux, Windows ve macOS üzerinde çalışır. Python ≥ 3.11
gerekir.*

1. **Firmware'i bir kez flash edin.** Arduino IDE şart değil; paketle gelen
   hazır firmware'i doğrudan host bilgisayardan yükleyebilirsiniz. Komut seri
   portları listeler, siz birini seçersiniz ve esptool ile *Huge APP* imajını
   yazar:

   ```sh
   uvx --from "python-esp-bridge[flash]" flash   # uv ile kurmadan çalıştırma
   ```

   Kendiniz derlemek isterseniz Arduino IDE Library Manager'dan
   **`python esp bridge`** kütüphanesini kurun, *File → Examples →
   python esp bridge → Bridge* örneğini açın, partition scheme olarak
   *Huge APP* seçin ve Upload'a basın. Sketch yalnızca `EspBridge.usb.begin();`
   `EspBridge.ble.begin(); EspBridge.run();` satırlarından oluşur
   çağrısından ibarettir. Ayrıntılar: [`FIRMWARE.md`](FIRMWARE.md).

2. **Python kütüphanesini** Pi'ye ya da PC'ye kurun. pip ile:

   ```sh
   pip install python-esp-bridge            # USB + Bluetooth birlikte gelir
   pip install "python-esp-bridge[oled]"    # + OLED ekranlar için Pillow
   pip install "python-esp-bridge[mcp]"     # + MCP sunucusu (espbridge-mcp)
   pip install "python-esp-bridge[flash]"   # + `espbridge flash` için esptool
   ```

   ...veya [uv](https://docs.astral.sh/uv/) ile:

   ```sh
   uv add python-esp-bridge                 # uv projesine ekler (USB + Bluetooth)
   uv add "python-esp-bridge[oled]"         # ekstra paketle (oled / mcp / all)
   uv pip install python-esp-bridge         # ya da aktif ortama kurar
   ```

   Bluetooth ek paket gerektirmeden çalışır. Eski `[ble]` ekstra seçeneği,
   geriye dönük uyumluluk için etkisiz olarak korunur.

3. **Başlayın:**

   ```python
   from espbridge import Bridge

   with Bridge() as esp:                      # önce Bluetooth, olmazsa USB serial
       print(esp.info)                        # çip, MAC, yetenekler

       esp.gpio.mode(2, "output")             # RPi GPIO gibi, ama ESP32 üstünde
       esp.gpio.write(2, 1)                    # pinin geri okunan seviyesini döndürür
       esp.gpio.write(2, 1, verify=True)      # yazma tutmadıysa hata verir
       print(esp.adc.read_mv(34), "mV")
       esp.dac.write(25, 128)                 # gerçek analog çıkış (klasik ESP32)
       esp.pwm.servo(13, angle=90)

       esp.i2c.init(sda=21, scl=22)
       print([hex(a) for a in esp.i2c.scan()])

       esp.wifi.connect("ssid", "password")   # ESP32'nin radyosu...
       status, body = esp.net.http_get("http://example.com/")  # ...modeminiz olur
   ```

   **USB kablosu olmadan** da kullanabilirsiniz. Kartlar `espbridge_<name>` adıyla
   yayın yapar ve parola ister. Varsayılan parola `espbridge`; sketch içinde
   `EspBridge.ble.begin("yourpassword")` ile değiştirilebilir:

   ```python
   with Bridge(ble=True, password="espbridge") as esp:   # Bluetooth üzerinden
       esp.gpio.write(2, 1)
   ```

   `Bridge()` önce Bluetooth'u dener, sonra USB serial'a düşer. Her taşıma
   anahtarı kendi bağlantısını sabitler: `ble=False` yalnızca **USB / COM**,
   `ble=True` yalnızca Bluetooth, `wifi=True` yalnızca Wi-Fi. Belirli bir kartı
   seçmek için `Bridge("relays")`, belirli bir seri port için `port="COM7"`
   verin.

   Komut satırında `espbridge` bağlantı bilgisini yazdırır; `espbridge ports`
   aday seri portları listeler; `espbridge scan` bağlı kartları yoklar,
   `espbridge scan --ble` ise Bluetooth üzerinden yayın yapan bridge'leri bulur.

## Özellikler

| modül | öne çıkanlar |
|-------|--------------|
| GPIO | pull-up/down ve open-drain dahil pin modları, toplu yazma, doğrulama için geri okunan seviye (`verify=` uyuşmazlıkta hata verir), debounce'lu kenar kesmeleri → Python callback'leri |
| ADC | ham okuma + kalibre mV, attenuation ayarı (ADC2/Wi-Fi çakışması korunur) |
| DAC | 8-bit çıkış + donanımsal cosine generator (klasik ESP32 / S2) |
| PWM | LEDC: herhangi bir pin, frekans/çözünürlük, `duty_pct`, `tone`, `servo` |
| Touch | kapasitif touch pad okumaları |
| I2C | 2 bus, tarama, yazma/okuma, register yardımcıları, repeated-start |
| SPI | 2 host, full-duplex transferler, CS yönetimi |
| UART | UART1/2 köprülenir: Python'dan yazma, RX tarafı olay olarak geri akar |
| Wi-Fi | tarama, STA bağlantısı, AP modu, durum/RSSI, durum olayları |
| NET | ESP32 radyosu üzerinden TCP client/server + UDP, socket benzeri API, credit-window akış kontrolü |
| BLE | tarama, advertise, GATT server (notify/write callback'leri), GATT client |
| ESP-NOW | bağlantısız ESP32↔ESP32 mesajlaşma: peer'ler + broadcast, teslim ACK'leri, RSSI ile RX, PMK/LMK şifreleme; Wi-Fi ve BLE ile birlikte çalışır |
| RMT | genel amaçlı darbe dizisi oynatma/yakalama; `neopixel`, `ir`, `dht`, `hcsr04`, `stepper` sürücülerinin ortak temeli |
| 1-Wire | herhangi bir pinde bus primitive'leri; ROM search + CRC8 Python'da (`esp.onewire`, DS18B20 sürücüsü dahil) |
| CAN | TWAI controller: 25k-1M bit/s, filtreler, send/recv + callback'ler (`esp.can`; transceiver çipi gerekir) |
| I2S | MEMS mikrofonlar ve DAC/amp'ler için PCM giriş/çıkış (`esp.i2s`; bağlantı bant genişliği yaklaşık 16-bit/32 kHz mono ile sınırlar) |
| Files | dahili flash üzerinde LittleFS + SD kartlar: open/read/write/list/... (`esp.fs`) |
| NVS | kart üzerinde kalıcı key/value depolama (`esp.nvs`) |
| Watch | kart üstü kurallar: kart ADC/GPIO/touch/heap değerlerini kendi örnekler, koşul tetiklenince olay gönderir ve host bağlantısına bağlı kalmadan yaklaşık 5 ms içinde **kart üzerinde tepki verebilir** (`do=("gpio", pin, level)` / `do=("pwm", pin, duty)`) |
| Sleep | timer/GPIO uyandırmalı deep + light sleep (`esp.deep_sleep()`; çip notlarına bakın) |
| Power | `esp.radio_off()`: Wi-Fi + ESP-NOW + tüm BT stack'i kapanır; radyo kesmeleri kalkar, yaklaşık 110 KB heap boşalır, USB üzerinden jitter hassas gerçek zamanlı işler için ADC2 pinleri açılır; `esp.cpu_freq()`, `esp.power_mode()` |
| OTA | firmware'i USB veya Bluetooth üzerinden yeniden flash etme (`esp.ota.flash("fw.bin")`; dual-app partition scheme) |
| Ethernet | RMII (WT32-ETH01, Olimex POE...) veya SPI (W5500); NET soketleri otomatik olarak bunun üzerinden çalışır (firmware'de etkinleştirme gerekir) |
| Camera | ESP32-CAM / XIAO-S3-Sense / ESP-EYE'dan JPEG görüntü (firmware'de etkinleştirme, PSRAM gerekir) |
| MCPWM | H-bridge'ler için donanımsal deadtime'lı tamamlayıcı PWM çifti (`esp.mcpwm`; S2/C3'te yok) |

**Saf Python cihaz sürücüleri** RMT/1-Wire/I2C primitive'leri üzerine kuruludur;
kendi sürücünüzü eklemek için firmware değiştirmeniz gerekmez:

```python
from espbridge.drivers.neopixel import NeoPixel   # WS2812/SK6812 şeritler
from espbridge.drivers.dht import DHT             # DHT11/DHT22 sıcaklık+nem
from espbridge.drivers.ds18b20 import DS18B20     # 1-Wire termometreler (multi-drop)
from espbridge.drivers.hcsr04 import HCSR04       # ultrasonik mesafe ölçümü
from espbridge.drivers.ir import IrSender, IrReceiver  # NEC kumandalar + ham IR
from espbridge.drivers.stepper import Stepper     # rampalı A4988/DRV8825

NeoPixel(esp, pin=5, n=30).fill((0, 0, 64))
print(DHT(esp, 4).read())                 # (23.1, 65.5)
Stepper(esp, step_pin=12, dir_pin=14).move(400, speed=800, accel=1600)
```

### Kendi sürücünüzü getirin

Bunlar **referans uygulamalardır, sınır değildir.** Paketle gelen her sürücü
[`espbridge/drivers/`](../python/espbridge/drivers/) altında durur. Bir sürücü,
constructor'ı ilk argüman olarak bridge alan ve yukarıdaki primitive'lerle
cihazla konuşan normal bir Python sınıfıdır. Örneğin
[`drivers/dht.py`](../python/espbridge/drivers/dht.py) 75 satırdır. Yani
herhangi bir sensör, ekran ya da protokol host tarafında yazılmış bir sınıfla
eklenebilir; **firmware değişmez**:

```python
class MyTempSensor:                          # bridge'i ilk alan herhangi bir sınıf
    def __init__(self, esp, address=0x48):
        self._i2c, self._addr = esp.i2c, address
    def read_c(self):
        hi, lo = self._i2c.read_reg(self._addr, 0x00, 2)
        return ((hi << 8 | lo) >> 4) * 0.0625

MyTempSensor(esp).read_c()                    # olduğu gibi çalışır, kayıt gerekmez
```

`esp.<name>(...)` kısayolu için bir ad kaydedebilir ya da başkalarının kurduğu
anda sürücünüzün her bridge üzerinde görünmesini sağlayan bir pip paketi
dağıtabilirsiniz:

```python
from espbridge import register_driver
register_driver("mytemp", MyTempSensor)
esp.mytemp(address=0x48).read_c()             # == MyTempSensor(esp, address=0x48)
```

`espbridge drivers` kullanılabilen her şeyi listeler: paketle gelen
[`drivers/`](../python/espbridge/drivers/) ve kurulu eklentiler. Tam rehber:
[**`DRIVERS.md`**](DRIVERS.md). Adafruit / luma / gpiozero / smbus2
ekosistemlerinde hazır bir sürücü varsa, [uyumluluk katmanları](#zaten-bildiginiz-kutuphaneleri-kullanin)
üzerinden değiştirmeden çalışır; yeniden yazmanız gerekmez.

Firmware FreeRTOS üzerinde tamamen olay güdümlüdür. Serial TX, komut işleme ve
ağ yığını ayrı task'lerde çalışır; bu yüzden bloklayan bir Wi-Fi/BLE işlemi
GPIO okumasını geciktirmez. Tipik gidiş-dönüş yaklaşık 1 ms'dir. Bağlantı,
USB bridge çipinin desteklediği hıza otomatik yükselir: CP210x'te 1.5 Mbaud,
CH340'ta 2 Mbaud.

## Eşzamanlılık ve entegrasyon

Bir kartın bağlantısı aynı anda iki kez açılamaz; ama **tek bir `Bridge`
thread-safe'tir**. Aynı bridge'i thread'ler arasında paylaşabilirsiniz.
İstekler kablo üzerinde pipeline edilir ve sequence number ile eşlenir; bir
thread'deki yavaş çağrı başka bir thread'deki hızlı çağrıyı bekletmez. Firmware
tarafında da buna karşılık gelen task ayrımı vardır; örnek için
[`rtos_concurrency.py`](../python/examples/basics/rtos_concurrency.py).

Kolay entegrasyon için her yere `Bridge` taşımayın. İhtiyacınız olan yerde
`connect()` çağırın; aynı paylaşılan, otomatik yeniden bağlanan bağlantıyı
alırsınız:

```python
import espbridge

esp = espbridge.connect(ble=False)      # her thread/modülden aynı canlı bağlantı
esp.gpio.write(2, 1)                     # eşzamanlı çağrılar için güvenli

# örn. FastAPI/Flask route'u; tüm istekler tek bağlantıyı paylaşır:
@app.get("/adc/{pin}")
def read(pin: int):
    return {"mV": espbridge.connect(ble=False).adc.read_mv(pin)}
```

`await` kullanıyorsanız herhangi bir bridge'i sarın ve eşzamanlı I/O'yu
`asyncio.gather` ile dağıtın
([`async_fanout.py`](../python/examples/basics/async_fanout.py)):

```python
from espbridge import AsyncBridge

async with AsyncBridge(ble=False) as esp:        # veya AsyncBridge.wrap(espbridge.connect())
    t, h = await asyncio.gather(esp.adc.read(34), esp.adc.read(35))
```

Birden çok process gerekiyorsa bağlantının sahibi tek process olsun; örneğin
[MCP](#bir-ai-agent-uzerinden-kullanin-mcp) ya da bir HTTP sunucusu. Diğer
process'ler onunla konuşsun. Bkz.
[`shared_connection.py`](../python/examples/basics/shared_connection.py).

## Zaten bildiğiniz kütüphaneleri kullanın

espbridge popüler Python donanım ekosistemlerinin wire protokollerini konuşur.
Bu yüzden mevcut kodlar, sürücüler ve eğitimler değişmeden çalışır; sadece
Raspberry Pi pinlerinin yerini ESP32 pinleri alır.

**gpiozero** - tam pin factory (LED, Button, PWMLED, edge callback'leri, ...):

```python
from gpiozero import LED, Button
from espbridge.compat.gpiozero import EspBridgeFactory

factory = EspBridgeFactory(esp)
led, btn = LED(2, pin_factory=factory), Button(4, pin_factory=factory)
btn.when_pressed = led.toggle
```

**Adafruit CircuitPython sürücüleri** - yüzlerce sensör/ekran için
busio/digitalio uyumlu I2C, SPI ve DigitalInOut:

```python
from adafruit_bme280.basic import Adafruit_BME280_I2C
from espbridge.compat.blinka import I2C

bme = Adafruit_BME280_I2C(I2C(esp))     # sürücü köprü üstünden çalıştığını bilmez
print(bme.temperature)
```

**smbus2** - klasik Pi I2C kodu, değiştirmeden:

```python
from espbridge.compat.smbus import SMBus
bus = SMBus(esp)                        # smbus2.SMBus(1) yerine
temp = bus.read_byte_data(0x48, 0x00)
```

**luma.oled / luma.lcd** I2C ve SPI ekran arayüzleri (`LumaI2C`, `LumaSPI`),
**RPi.GPIO** için `espbridge.compat.rpi_gpio` uyumluluk katmanı ve native
nesneler de stdlib alışkanlıklarını izler: UART portları pyserial benzeridir
(`in_waiting`, `readline`), köprülenmiş TCP/UDP soketleri
`settimeout`/`recv`/`sendall` destekler.

I2C OLED'ler (SSD1306 / SH1106 / yaygın klonları) doğrudan desteklenir.
`pip install "python-esp-bridge[oled]"` kurun ve PIL ile çizin:

```python
from espbridge.drivers.oled import OLED

oled = OLED(esp)                # bus init + auto-detect + klonlara güvenli power-up
with oled.draw() as d:          # d bir PIL ImageDraw'dur
    d.text((0, 10), "Hello!", fill="white")
```

## Bir AI agent üzerinden kullanın (MCP)

Bridge'in tamamını bir [Model Context Protocol](https://modelcontextprotocol.io)
sunucusu olarak LLM'e açabilirsiniz. 100'den fazla araç GPIO, ADC/DAC, PWM,
I2C, SPI, UART, Wi-Fi, NVS, dosya sistemi, 1-Wire, ESP-NOW, CAN, MCPWM,
Ethernet, kamera ve OTA'yı kapsar. Agent sensör okuyabilir, pin değiştirebilir,
I2C tarayabilir ve bunları doğal dille yönetebilir.

Sunucuyu bir kez kurun, sonra kartı takın; port otomatik algılanır:

```bash
uv tool install "python-esp-bridge[mcp]"     # veya: pip install "python-esp-bridge[mcp]"
```

**Claude Code, Gemini CLI, Codex CLI, Antigravity, Cursor/Windsurf ve Ollama**
ile çalışır. Hepsi aynı `espbridge-mcp` komutunu başlatır. Bu repo Claude Code
(`.mcp.json`) ve Gemini CLI (`.gemini/settings.json`) için hazır ayarlarla
gelir; asistanı repo içinde açmanız yeterlidir. Diğer istemciler aynı tek
satırlık yapılandırmayı kullanır (`mcpServers` anahtarı):

```jsonc
{ "mcpServers": {
    "espbridge": { "command": "espbridge-mcp", "args": [] }
} }
```

Araçlar çevre birimine göre gruplanır (`gpio_*`, `i2c_*`, `wifi_*`, ...);
ham byte payload'ları hex string olarak girer ve çıkar. Kendi sunucunuza
gömmek için `from espbridge.mcp import build_server` kullanın. **Codex,
Antigravity ve Ollama dahil asistan bazlı kurulum: [`MCP.md`](MCP.md).**

### Birden çok ESP32

Her karta bir kez ad verin (`espbridge -p COM7 set-name relays`); ad kartın
flash'ına yazılır, yeniden başlatmadan ve port numarası değişiminden
etkilenmez. Sonra ne portları ne de MAC'leri düşünmeniz gerekir:

```python
from espbridge import Bridge

esp = Bridge("relays")                    # tek ad  -> yalnızca o kart
esp = Bridge("c0:49:ef:d0:3f:e0")         # MAC de aynı argümanda çalışır

with Bridge(["relays", "sensors"]) as boards:   # liste -> tam olarak onlar
    boards["relays"].gpio.write(2, 1)

with Bridge() as boards:                  # seçici yok -> tüm kartlar
    boards.each(lambda esp: esp.ping())
```

Bu argüman kartın **kimliğidir**: adı, ad vermediyseniz MAC'i. Her iki değer de
Bluetooth reklamında, Wi-Fi keşif yanıtında ve `SYS_INFO` içinde tam olarak
taşındığı için USB, Bluetooth ve Wi-Fi üzerinde aynı şekilde çalışır. Asla bir
COM portu veya IP adresi değildir — onların kendi anahtarları var, yani dizenin
şeklinden hiçbir şey tahmin edilmez.

Adlar 16 karakterle sınırlıdır; bu, reklam edilen `espbridge_<name>` dizesini
Bluetooth tarama yanıtının aldığı ~26 karakterin içinde tutar. Daha uzun bir ad
havada kırpılacağı için baştan reddedilir. İstenen kartların hepsi bulunamazsa
sonuç kısmi bir çalışma değil, hatadır.

## Sorun giderme

Hatalar komut adını söyler ve neyi kontrol etmeniz gerektiğini belirtir:
`I2C_WRITE (0x4003) failed: IO — no ACK on the wire — check wiring, power,
device address and pull-ups`. Timeout durumunda karta ek olarak ping atılır;
mesaj, bağlantının mı koptuğunu yoksa tek bir frame'in mi kaybolduğunu söyler.
Yararlı ayarlar:

```python
esp = Bridge(retries=1)         # varsayılan: timeout'ta güvenli komutları bir kez tekrar gönderir
esp.free_heap()                 # firmware'den heap + düşen frame sayaçları
```

```bash
ESPBRIDGE_DEBUG=1 python app.py   # her istek/yanıtı adlarıyla izler
```

Yoğun bağlantıda frame kayıpları artık baştan engellenir: pipeline edilen
patlamalar (OLED frame'leri, NeoPixel güncellemeleri) USB serial ve Bluetooth
üzerinde firmware'in link buffer'ının kaldırabileceği hıza otomatik kısılır.

## Repo düzeni

Repo kökü **Arduino kütüphanesinin kendisidir**; bu sayede Arduino Library
Manager'a yayımlanabilir. Python paketi `python/` altında yer alır.

| yol | açıklama |
|-----|----------|
| [`../src/`](../src/) + [`../examples/Bridge/`](../examples/Bridge/) | Arduino kütüphanesi: bir kez flash edilen firmware (C/C++) + örnek sketch (`EspBridge.usb/ble/wifi.begin()`) |
| `../library.properties`, `../keywords.txt` | Arduino Library Manager metadata'sı (registry'nin istediği gibi repo kökünde) |
| [`../python/`](../python/) | Python paketi `python-esp-bridge` (`import espbridge`), kendi `tests/` klasörü ve gruplanmış `examples/` dizinleri (`basics/`, `devices/`, `system/`, `wireless/`, `network/`, `displays/`, `compat/`) |
| [`MCP.md`](MCP.md) | MCP sunucusu (`espbridge-mcp`): bridge'i bir AI agent üzerinden kullanma |
| [`PROTOCOL.md`](PROTOCOL.md) | binary wire protocol tanımı (framing, transport'lar, auth) |
| [`FIRMWARE.md`](FIRMWARE.md) | firmware flash etme, partition scheme ve build flag referansı |

## Desteklenen donanım

Ana hedef klasik **ESP32** DevKit'lerdir (ESP-32S / ESP-32D, 30 ve 38 pin,
CP2102/CH340 USB). **ESP32-S2/S3/C3/C6/H2** aynı sketch ile derlenir
(native USB; ESP-NOW her yerde çalışır; S3/C3/C6/H2'de DAC yoktur). Yetenekler
bağlantı sırasında firmware tarafından bildirilir; Python API, çipinizde
olmayan özellikler için hızlı ve açık hata verir.

Klasik ESP32 (CP2102) üzerinde **arduino-esp32 core 3.3.6** ile test edildi.
Firmware **Minimal SPIFFS** partition scheme ile flash edildi (1.9 MB app +
OTA; firmware bu slotun yaklaşık %95'ini kullanır). OTA gerekmiyorsa
`Huge APP` de çalışır. Doğrulananlar: 1.5 Mbaud USB, BLE bağlantısı, ESP-NOW
ve Wi-Fi/BLE/ESP-NOW birlikte çalışma. Kullanılan soak/coex paketi için
`python/examples/wireless/stress_test.py` dosyasına bakın.

> **Bluetooth notu:** arduino-esp32 core 3.x, S3/C3/C6/H2 üzerinde NimBLE host
> ile gelir. Bridge'in Bluetooth kodu (BLE bağlantısı + `esp.ble`) Bluedroid
> konuştuğu için bu çiplerde firmware şu an yalnızca USB olarak derlenir.
> Klasik ESP32 Bluedroid kullanmaya devam eder: tam BLE bağlantısı + Wi-Fi +
> ESP-NOW birlikte çalışır.

> **Klasik ESP32 IRAM ödünü:** Wi-Fi ve Bluetooth birlikte yüklendiğinde çipin
> instruction RAM'i dolar. Bu nedenle varsayılan klasik derleme SD kart
> desteğini (LittleFS çalışmaya devam eder) ve deep/light sleep'i atlar.
> SD + sleep'i geri almak için `BRIDGE_ENABLE_BLE 0` ile USB-only derleyin.
> S2/S3/C3/C6/H2'de bu özellikler her durumda vardır. Python API iki durumda
> da net bir `UnsupportedError` yükseltir (`Cap.SLEEP`, `Cap.SDMMC` sorguları).

### Çip bazlı destek matrisi (v0.3.5 modülleri)

| | ESP32 | S2 | S3 | C3 | C6 | H2 |
|---|---|---|---|---|---|---|
| RMT / 1-Wire / CAN / I2S / NVS / OTA | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| LittleFS | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| SD (SPI) / sleep | yalnızca BLE kapalı | ✓ | ✓ | ✓ | ✓ | ✓ |
| SDMMC slot | yalnızca BLE kapalı | — | ✓ | — | — | — |
| MCPWM (deadtime çifti) | ✓ | — | ✓ | — | ✓ | ✓ |
| Camera (opt-in) | ✓ (PSRAM) | ✓ (PSRAM) | ✓ (PSRAM) | — | — | — |
| Ethernet RMII (opt-in) | ✓ | — | — | — | — | — |
| Ethernet SPI W5500 (opt-in) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
