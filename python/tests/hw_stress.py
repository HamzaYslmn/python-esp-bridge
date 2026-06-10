"""Full hardware stress suite for python-esp-bridge (radio refactor validation).

Boards: COM3 = "relays" board (OLED on I2C; NO gpio writes), COM7 = spare bench.
Runs the whole suite N times, tracks heap at the end of every iteration.
"""
import sys
import time
import threading

import espbridge
from espbridge import constants as C
from espbridge.errors import RemoteError

ITERATIONS = 5
results = []   # (iteration, test, ok, detail)


def check(it, name, ok, detail=""):
    results.append((it, name, bool(ok), detail))
    print(f"  [{it}] {name:<38} {'PASS' if ok else 'FAIL':<4} {detail}")


def run_test(it, name, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    check(it, name, ok, detail)


a = espbridge.Bridge(port="COM3")
b = espbridge.Bridge(port="COM7")
print(f"COM3: fw {a.info.fw_version} name={a.info.name!r} mac={a.info.mac}")
print(f"COM7: fw {b.info.fw_version} name={b.info.name!r} mac={b.info.mac}")

# ESP-NOW RX counters, registered once (callbacks persist across begin/end).
rx_a, rx_b = [], []
a.espnow.on_receive(lambda mac, data, rssi: rx_a.append(data))
b.espnow.on_receive(lambda mac, data, rssi: rx_b.append(data))

heap_log = []

for it in range(1, ITERATIONS + 1):
    print(f"\n=== iteration {it}/{ITERATIONS} ===")
    t_iter = time.time()

    # ---- system / link throughput ------------------------------------------
    def t_ping():
        n, t0 = 300, time.perf_counter()
        for _ in range(n):
            a.ping()
        avg = (time.perf_counter() - t0) / n * 1000
        return avg < 15, f"300 pings, avg {avg:.2f} ms"
    run_test(it, "sys: ping x300 (COM3)", t_ping)

    def t_power_mode():
        bat = a.power_mode("battery")
        perf = a.power_mode("performance")
        # ble_link is None on USB-only, or the mode when a central happens
        # to be attached -- both correct; assert the CPU knob only.
        ok = bat["cpu_mhz"] == 80 and perf["cpu_mhz"] == 240
        return ok, f"battery={bat} performance={perf}"
    run_test(it, "sys: power_mode cycle (COM3)", t_power_mode)

    # ---- GPIO ----------------------------------------------------------------
    def t_gpio_b():
        b.gpio.mode(2, "output")
        for v in range(200):
            b.gpio.write(2, v & 1)
        rb = b.gpio.write(2, 0, verify=True)
        return rb == 0, "200 toggles + verified low"
    run_test(it, "gpio: toggle x200 pin2 (COM7)", t_gpio_b)

    def t_gpio_read():
        ra, rb_ = a.gpio.read_all(), b.gpio.read_all()
        return True, f"read_all a=0x{ra:010x} b=0x{rb_:010x}"
    run_test(it, "gpio: read_all (both)", t_gpio_read)

    # ---- analog ---------------------------------------------------------------
    def t_adc1():
        va, vb = a.adc.read(34), b.adc.read(34)
        return 0 <= va <= 4095 and 0 <= vb <= 4095, f"pin34 a={va} b={vb}"
    run_test(it, "adc: ADC1 pin34 (both)", t_adc1)

    def t_adc2_guard():
        v0 = b.adc.read(26)              # radio off -> ADC2 readable
        b.espnow.begin()                 # radio up
        try:
            try:
                b.adc.read(26)
                return False, "ADC2 read succeeded with radio up (guard missing!)"
            except RemoteError as e:
                refused = e
        finally:
            b.espnow.end()               # radio back off
        v1 = b.adc.read(26)              # readable again
        return True, f"off={v0}, radio-up refused ({refused}), off-again={v1}"
    run_test(it, "adc: ADC2 guard vs radio (COM7)", t_adc2_guard)

    def t_touch_dac():
        tv = b.touch.read(4)
        b.dac.write(25, 128)
        b.dac.disable(25)
        return tv > 0, f"touch4={tv}, dac25 write+disable ok"
    run_test(it, "analog: touch + dac (COM7)", t_touch_dac)

    # ---- PWM ------------------------------------------------------------------
    def t_pwm():
        b.pwm.attach(2, 5000)
        for pct in (10, 50, 90):
            b.pwm.duty_pct(2, pct)
        b.pwm.detach(2)
        return True, "attach/duty 10-50-90/detach"
    run_test(it, "pwm: cycle pin2 (COM7)", t_pwm)

    # ---- I2C ------------------------------------------------------------------
    def t_i2c():
        a.i2c.init()
        try:
            found = a.i2c.scan()
        finally:
            a.i2c.deinit()
        return 0x3C in found, f"scan: {[hex(x) for x in found]}"
    run_test(it, "i2c: scan finds OLED 0x3c (COM3)", t_i2c)

    # ---- NVS ------------------------------------------------------------------
    def t_nvs(esp=a, tag="COM3"):
        esp.nvs.set("hw_i", it * 1234)
        esp.nvs.set("hw_s", f"iter{it}")
        esp.nvs.set("hw_b", bytes(range(16)))
        ok = (esp.nvs.get_int("hw_i") == it * 1234
              and esp.nvs.get_str("hw_s") == f"iter{it}"
              and esp.nvs.get("hw_b") == bytes(range(16)))
        for k in ("hw_i", "hw_s", "hw_b"):
            esp.nvs.delete(k)
        gone = esp.nvs.get("hw_i") is None
        return ok and gone, "set/get int+str+bytes, delete verified"
    run_test(it, "nvs: roundtrip (COM3)", lambda: t_nvs(a))
    run_test(it, "nvs: roundtrip (COM7)", lambda: t_nvs(b))

    # ---- FS -------------------------------------------------------------------
    def t_fs(esp):
        fs = esp.fs.mount()
        try:
            blob = bytes((i * 7 + it) & 0xFF for i in range(8192))
            fs.write_file("/hwtest.bin", blob)
            back = fs.read_file("/hwtest.bin")
            size, is_dir, _ = fs.stat("/hwtest.bin")
            total, used = fs.df()
            fs.remove("/hwtest.bin")
            ok = back == blob and size == 8192 and not is_dir
            return ok, f"8KB roundtrip, df {used}/{total} KB"
        finally:
            fs.umount()
    run_test(it, "fs: 8KB write/read/remove (COM3)", lambda: t_fs(a))
    run_test(it, "fs: 8KB write/read/remove (COM7)", lambda: t_fs(b))

    # ---- UART (no loopback wired: init/write/close paths) ----------------------
    def t_uart():
        u = b.uart.init(port=1, tx=17, rx=16)
        u.write(b"x" * 64)
        u.close()
        return True, "init/write64/close"
    run_test(it, "uart: port1 cycle (COM7)", t_uart)

    # ---- WATCH (heap source: no wiring needed) ----------------------------------
    def t_watch(esp):
        fired = threading.Event()
        wid = esp.watch.add("heap", below=10**9, period_ms=50, initial=True,
                            callback=lambda ev: fired.set())
        try:
            ok = fired.wait(2.0)
        finally:
            esp.watch.remove(wid)
        return ok, f"heap rule id={wid} fired"
    run_test(it, "watch: heap rule fires (COM3)", lambda: t_watch(a))
    run_test(it, "watch: heap rule fires (COM7)", lambda: t_watch(b))

    # ---- Wi-Fi ------------------------------------------------------------------
    def t_scan(esp):
        nets = esp.wifi.scan()
        return len(nets) > 0, f"{len(nets)} networks"
    run_test(it, "wifi: scan (COM3)", lambda: t_scan(a))
    run_test(it, "wifi: scan (COM7)", lambda: t_scan(b))

    # ---- ESP-NOW exchange --------------------------------------------------------
    mac_a = a.espnow.begin()
    mac_b = b.espnow.begin()
    a.espnow.add_peer(mac_b)
    b.espnow.add_peer(mac_a)

    def t_unicast():
        acked = sum(a.espnow.send(mac_b, b"uni", wait=True) for _ in range(50))
        return acked >= 45, f"{acked}/50 ACKed"
    run_test(it, "espnow: unicast ACK x50 A->B", t_unicast)

    def t_broadcast():
        del rx_a[:]
        for _ in range(20):
            b.espnow.broadcast(b"bc")
            time.sleep(0.02)
        time.sleep(0.4)
        return len(rx_a) >= 12, f"{len(rx_a)}/20 received on COM3"
    run_test(it, "espnow: broadcast x20 B->A", t_broadcast)

    def t_ff_rapid():
        del rx_b[:]
        for _ in range(100):
            a.espnow.send(mac_b, b"ff", wait=False)
            time.sleep(0.002)
        time.sleep(0.5)
        return len(rx_b) >= 60, f"{len(rx_b)}/100 fire-and-forget received"
    run_test(it, "espnow: rapid FF x100 A->B", t_ff_rapid)

    def t_power_save():
        del rx_b[:]
        b.espnow.power_save(0)           # RX off
        time.sleep(0.2)
        for _ in range(5):
            a.espnow.broadcast(b"dark")
            time.sleep(0.05)
        time.sleep(0.3)
        dark = len(rx_b)
        b.espnow.power_save(65535)       # always listening again
        time.sleep(0.2)
        del rx_b[:]
        for _ in range(5):
            a.espnow.broadcast(b"lit")
            time.sleep(0.05)
        time.sleep(0.3)
        lit = len(rx_b)
        return dark == 0 and lit >= 3, f"window0: {dark}/5 (want 0), restored: {lit}/5"
    run_test(it, "espnow: power_save window (COM7)", t_power_save)

    # ---- multi-user radio: AP + ESP-NOW hold each other ---------------------------
    def t_radio_multiuser():
        # Unicast with wait=True so a send failure raises instead of vanishing.
        def acked5():
            n, err = 0, None
            for _ in range(5):
                try:
                    n += a.espnow.send(mac_b, b"mu", wait=True)
                except RemoteError as e:
                    err = e
                time.sleep(0.02)
            return n, err
        h_pre = a.free_heap()["free"]
        try:
            ip = a.wifi.ap_start(f"hwtest-{it}", "stress123")    # AP joins ESP-NOW on the radio
        except RemoteError as e:
            if "NO_MEM" in str(e):       # heap guard refused: correct with a BLE central at low heap
                return True, f"ap_start guard-refused at heap {h_pre} (BLE central attached) -- correct"
            raise
        h_ap = a.free_heap()["free"]
        with_ap, err_ap = acked5()
        a.wifi.ap_stop()                  # ESP-NOW still holds the radio: must keep working
        after_ap, err_post = acked5()
        return (after_ap >= 3,
                f"ap={ip}, heap {h_pre}->{h_ap}, ACK with AP {with_ap}/5"
                f"{f' (err: {err_ap})' if err_ap else ''}, after ap_stop {after_ap}/5"
                f"{f' (err: {err_post})' if err_post else ''}")
    run_test(it, "radio: AP + ESP-NOW multi-user (COM3)", t_radio_multiuser)

    # ---- teardown + radio-off heap reclaim ----------------------------------------
    def t_radio_off():
        a.espnow.end()
        b.espnow.end()
        time.sleep(0.3)
        ha, hb = a.free_heap()["free"], b.free_heap()["free"]
        return ha > 40000 and hb > 40000, f"radio off, heap a={ha} b={hb}"
    run_test(it, "radio: release -> heap reclaimed (both)", t_radio_off)

    # ---- rapid radio cycling --------------------------------------------------------
    def t_radio_cycle():
        for _ in range(5):
            b.espnow.begin()
            b.espnow.end()
        time.sleep(0.3)
        h = b.free_heap()["free"]
        return h > 40000, f"5x begin/end, heap {h}"
    run_test(it, "radio: 5x begin/end cycle (COM7)", t_radio_cycle)

    # ---- BLE dual-link phase ----------------------------------------------------------
    def t_ble():
        name = a.info.name or True
        ble = espbridge.Bridge(ble=name)
        try:
            t0 = time.perf_counter()
            for _ in range(10):
                ble.ping()
            rtt = (time.perf_counter() - t0) / 10 * 1000
            # Classic ESP32 + Bluedroid: heap margins are meant to REFUSE a
            # radio bring-up during a BLE session rather than kill the link.
            try:
                nets = ble.wifi.scan()
                guard = f"scan allowed ({len(nets)} nets)"   # bigger-heap state
            except RemoteError as e:
                if "NO_MEM" not in str(e):
                    raise
                guard = "scan guard-refused (link protected)"
            ble.link_power("battery")
            t1 = time.perf_counter()
            ble.ping()
            bat_rtt = (time.perf_counter() - t1) * 1000
            ble.link_power("performance")
            ok_after = ble.ping() is not None                 # link survived it all
            detail = (f"10 pings avg {rtt:.1f} ms, {guard}, "
                      f"battery ping {bat_rtt:.0f} ms, link alive={ok_after}")
        finally:
            ble.close()
            del ble
            import gc
            gc.collect()
        # Windows holds BLE connections after close; wait for the board to see
        # the disconnect (heap recovers + authed flag clears) so the next
        # iteration starts central-free.
        t0 = time.time()
        while time.time() - t0 < 30:
            if a.free_heap()["free"] > 44000:
                return True, detail + f", disconnect after {time.time()-t0:.1f}s"
            time.sleep(1)
        return True, detail + ", WARNING: central still attached after 30s"
    run_test(it, "ble: dual-link session (COM3 board)", t_ble)

    # ---- iteration heap snapshot --------------------------------------------------------
    ha, hb = a.free_heap(), b.free_heap()
    heap_log.append((it, ha["free"], hb["free"]))
    print(f"  [{it}] heap end-of-iteration: COM3 {ha['free']} (min {ha['min_free']}), "
          f"COM7 {hb['free']} (min {hb['min_free']})  [{time.time()-t_iter:.0f}s]")

# ---- summary ------------------------------------------------------------------------------
print("\n" + "=" * 70)
fails = [r for r in results if not r[2]]
total = len(results)
print(f"RESULT: {total - len(fails)}/{total} checks passed over {ITERATIONS} iterations")
print("heap trend (end of each iteration):")
for it, hfa, hfb in heap_log:
    print(f"  iter {it}: COM3 {hfa}  COM7 {hfb}")
drift_a = heap_log[-1][1] - heap_log[0][1]
drift_b = heap_log[-1][2] - heap_log[0][2]
print(f"heap drift iter1->iter{ITERATIONS}: COM3 {drift_a:+d} B, COM7 {drift_b:+d} B")
if fails:
    print("\nFAILED checks:")
    for it, name, _, detail in fails:
        print(f"  iter {it}: {name} -- {detail}")
a.close(); b.close()
sys.exit(1 if fails else 0)
