"""Single-board stress + BLE speed test for COM3 (relays/OLED board).

Validates the restructured (src/esp + src/nrf) firmware on real ESP32 hardware.
No second board, so ESP-NOW/peer tests are skipped. No relay-pin writes — only
the onboard LED (GPIO2), ADC, I2C(OLED), PWM, NVS, and heap/leak tracking.
"""
import time
import espbridge

PORT = "COM3"
LED = 2          # onboard LED — safe to drive on this board
ADC_PIN = 34     # ADC1, input-only
OLED = 0x3c
ITER = 10

results = []
def chk(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {name:<34} {'PASS' if ok else 'FAIL':<4} {detail}")

e = espbridge.Bridge(port=PORT)
print(f"{PORT}: fw {e.info.fw_version} chip {e.info.chip.name} mac {e.info.mac}")
h0 = e.free_heap()["free"]
print(f"start heap = {h0}\n")

# ---- USB ping throughput ----------------------------------------------------
N = 300
t0 = time.perf_counter()
for _ in range(N):
    e.ping()
dt = time.perf_counter() - t0
avg_ms = dt / N * 1000
chk("usb ping x300", avg_ms < 15, f"avg {avg_ms:.2f} ms, {N/dt:.0f} req/s")

# ---- I2C init + OLED present ------------------------------------------------
e.i2c.init(sda=21, scl=22, freq=400000, bus=0)
found = e.i2c.scan(0)
chk("i2c scan finds OLED 0x3c", OLED in found, f"addrs={[hex(a) for a in found]}")

# ---- stress loop ------------------------------------------------------------
print(f"\n--- {ITER} stress iterations ---")
heaps = []
for it in range(1, ITER + 1):
    ok = True; why = ""
    try:
        # ADC
        e.adc.read(ADC_PIN); e.adc.read_mv(ADC_PIN)
        # I2C: scan + a harmless SSD1306 NOP (0xE3) write to exercise the write path
        if OLED not in e.i2c.scan(0): ok, why = False, "oled vanished"
        e.i2c.write(OLED, bytes([0x00, 0xE3]), bus=0)
        # PWM on the LED pin
        e.pwm.attach(LED, freq=1000, resolution_bits=10)
        e.pwm.write(LED, 512); e.pwm.write(LED, 0)
        e.pwm.detach(LED)
        # NVS round-trip
        e.nvs.set("stress", bytes([it, 0xAA, 0x55]))
        if e.nvs.get("stress") != bytes([it, 0xAA, 0x55]): ok, why = False, "nvs mismatch"
        e.nvs.delete("stress")
        # GPIO read (no relay writes)
        e.gpio.read(LED)
        # 100 pings of link pressure
        for _ in range(100): e.ping()
    except Exception as ex:
        ok, why = False, f"{type(ex).__name__}: {ex}"
    fh = e.free_heap()
    heaps.append(fh["free"])
    chk(f"iter {it:>2}", ok, f"heap {fh['free']}  drops={fh['dropped_events']} rxerr={fh['serial_rx_errors']}  {why}")

leak = heaps[0] - heaps[-1]
chk("no heap leak over iterations", abs(leak) < 2000, f"delta {leak} B ({heaps[0]} -> {heaps[-1]})")
fh = e.free_heap()
chk("zero dropped events", fh["dropped_events"] == 0, f"dropped={fh['dropped_events']}")
chk("zero serial rx errors", fh["serial_rx_errors"] == 0, f"rxerr={fh['serial_rx_errors']}")

e.close()
time.sleep(5)  # Windows lingers the USB/BLE stack briefly after close

# ---- BLE speed test ---------------------------------------------------------
print("\n--- BLE speed test ---")
try:
    b = espbridge.Bridge(ble=True, password="espbridge")
    print(f"BLE connected: {b.info.chip.name} mac {b.info.mac}")
    # warm up
    for _ in range(5): b.ping()
    M = 100
    lat = []
    t0 = time.perf_counter()
    for _ in range(M):
        s = time.perf_counter(); b.ping(); lat.append((time.perf_counter()-s)*1000)
    dt = time.perf_counter() - t0
    lat.sort()
    chk("ble ping x100", True,
        f"{M/dt:.1f} req/s  avg {sum(lat)/M:.1f} ms  p50 {lat[M//2]:.1f}  p95 {lat[int(M*0.95)]:.1f} ms")
    # throughput: SYS_INFO reads (larger payload) to gauge sustained bytes
    K = 50
    t0 = time.perf_counter()
    for _ in range(K): b.free_heap()
    chk("ble free_heap x50", True, f"{K/(time.perf_counter()-t0):.1f} req/s")
    b.close()
except Exception as ex:
    chk("ble connect", False, f"{type(ex).__name__}: {ex}")

# ---- summary ----------------------------------------------------------------
passed = sum(1 for _, ok in results if ok)
print(f"\n==== {passed}/{len(results)} checks passed ====")
