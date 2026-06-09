"""How to use the firmware's FreeRTOS architecture from Python.

The bridge firmware runs three FreeRTOS tasks on the ESP32 (see
docs/PROTOCOL.md):

    bridge_tx   — owns the serial port, drains the outbound frame queue
    bridge_rx   — executes FAST commands inline: GPIO/ADC/DAC/PWM/I2C/SPI/UART
    bridge_net  — owns Wi-Fi/NET/BLE; slow blocking calls live ONLY here

You don't start tasks yourself — you *exploit* them: requests sent from
different Python threads are correlated by sequence number, so fast traffic
overtakes slow traffic on the wire. A TCP connect that blocks bridge_net for
seconds costs GPIO/ADC traffic nothing.

This demo drives all three tasks at once and proves the fast lane stays fast:

    thread A: blocking network operations (bridge_net task)
    thread B: PWM breathing LED           (bridge_rx task)
    thread C: live OLED status redraws    (bridge_rx task, over I2C)
    main:     latency-samples ping+ADC    (bridge_rx task)

It runs over **USB on purpose** (ble=False): the demo's whole point is that the
slow lane can't stall the fast lane, and the USB wire is independent of the
ESP32 radio. Over Bluetooth the Wi-Fi workload would contend with the BLE link
itself (radio coexistence), muddying the result — that's a radio limitation,
not the task split.

Every worker degrades gracefully: a pin that can't do PWM, a missing OLED, or a
dropped frame is reported and skipped — one lane failing never takes down the
demo. Tune the run length with the RTOS_SECONDS env var (default 15).

    uv run basics/rtos_concurrency.py            # Wi-Fi scans as the slow load
    uv run basics/rtos_concurrency.py SSID PASS  # real TCP traffic as the load
"""
import os
import statistics
import sys
import threading
import time

from espbridge import Bridge, BridgeError

SSID = sys.argv[1] if len(sys.argv) > 1 else ""
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else ""
DURATION = float(os.environ.get("RTOS_SECONDS", "15"))

LED = 2
ADC_PIN = 34


def slow_network_work(esp: Bridge, stop: threading.Event) -> None:
    """Keep the bridge_net task busy with seconds-long blocking calls."""
    if SSID:
        print("[net] joining Wi-Fi ...")
        st = esp.wifi.connect(SSID, PASSWORD)
        print(f"[net] connected: {st.ip}")
        while not stop.is_set():
            try:
                # TCP connect blocks bridge_net for up to 5 s — fast lane unaffected.
                with esp.net.tcp_connect("example.com", 80) as s:
                    s.send(b"HEAD / HTTP/1.0\r\nHost: example.com\r\n\r\n")
                    s.recv(timeout=10)
                print("[net] HTTP round-trip done")
            except BridgeError as e:
                print(f"[net] {e}")
            stop.wait(1.0)
    else:
        print("[net] no SSID given — using Wi-Fi scans as the slow workload")
        while not stop.is_set():
            try:
                nets = esp.wifi.scan()       # several seconds inside bridge_net
                print(f"[net] scan finished: {len(nets)} networks")
            except BridgeError as e:
                print(f"[net] scan failed: {e}")
                stop.wait(0.5)


def breathing_led(esp: Bridge, stop: threading.Event) -> None:
    """Smooth PWM fade — handled by bridge_rx, immune to network stalls.
    Skips cleanly if this pin can't drive LEDC PWM on this board."""
    try:
        esp.pwm.attach(LED, freq=1000, resolution_bits=10)
    except BridgeError as e:
        print(f"[led] PWM unavailable on GPIO{LED} ({e}) — skipping")
        return
    print(f"[led] breathing GPIO{LED}")
    try:
        while not stop.is_set():
            for pct in list(range(0, 101, 4)) + list(range(100, -1, -4)):
                if stop.is_set():
                    break
                esp.pwm.duty_pct(LED, pct)
                time.sleep(0.02)
    finally:
        try:
            esp.pwm.detach(LED)          # always release the LEDC channel
        except BridgeError:
            pass


def oled_status(esp: Bridge, stop: threading.Event) -> None:
    """Live OLED redraws over I2C — also bridge_rx, so the screen keeps updating
    while the network task blocks bridge_net. Skips cleanly if there's no panel."""
    try:
        from espbridge.drivers.oled import OLED
    except ImportError:
        print("[oled] Pillow not installed "
              "(pip install 'python-esp-bridge[oled]') — skipping")
        return
    try:
        oled = OLED(esp)                 # I2C init + auto-detect + power up
    except (BridgeError, OSError) as e:  # no panel wired, or bus busy
        print(f"[oled] no display ({e}) — skipping")
        return

    print("[oled] driving display")
    t0 = time.monotonic()
    frames = 0
    try:
        while not stop.is_set():
            elapsed = time.monotonic() - t0
            with oled.draw() as d:
                d.text((0, 0), esp.info.chip.name, fill="white")
                d.text((0, 16), f"up    {elapsed:6.1f}s", fill="white")
                d.text((0, 28), f"frame {frames}", fill="white")
                d.rectangle((0, 56, int(127 * (elapsed % 5) / 5), 62), fill="white")
            frames += 1
            time.sleep(0.1)
    except BridgeError as e:
        print(f"[oled] stopped ({e})")
    print(f"[oled] {frames} frames drawn while the demo ran")


def _worker(fn, esp: Bridge, stop: threading.Event) -> None:
    """Run a worker so any error is reported cleanly, never as a raw traceback.
    One lane failing just drops that lane; the others keep running."""
    try:
        fn(esp, stop)
    except BridgeError as e:
        print(f"[{fn.__name__}] stopped: {e}")
    except Exception as e:  # noqa: BLE001 — a demo lane must not crash the process
        print(f"[{fn.__name__}] unexpected error: {e!r}")


def _pctl(xs: list[float], p: float) -> float:
    return sorted(xs)[min(len(xs) - 1, int(len(xs) * p))]


def main() -> None:
    # ble=False: drive this over USB — see the module docstring for why.
    with Bridge(ble=False) as esp:
        print(f"connected to {esp.info.chip.name} ({esp.info.name or esp.info.mac})\n")
        esp.adc.config(ADC_PIN, atten=11)

        stop = threading.Event()
        workers = (slow_network_work, breathing_led, oled_status)
        threads = [threading.Thread(target=_worker, args=(fn, esp, stop), daemon=True)
                   for fn in workers]
        for t in threads:
            t.start()

        # Sample fast-lane latency WHILE the network task is blocking. A dropped
        # frame on the wire is counted, not fatal — the demo keeps sampling.
        pings: list[float] = []
        adcs: list[float] = []
        misses = 0
        deadline = time.monotonic() + DURATION
        while time.monotonic() < deadline:
            try:
                pings.append(esp.ping() * 1000)
                t0 = time.perf_counter()
                esp.adc.read(ADC_PIN)
                adcs.append((time.perf_counter() - t0) * 1000)
            except BridgeError:
                misses += 1
            time.sleep(0.05)

        stop.set()
        for t in threads:
            t.join(timeout=5)

        if not pings:
            print("\nno fast-lane samples completed — is the board still connected?")
            return
        print(f"\nfast lane while the slow lane was busy ({len(pings)} samples):")
        print(f"  ping     avg {statistics.mean(pings):6.2f} ms   "
              f"p95 {_pctl(pings, 0.95):6.2f} ms   max {max(pings):6.2f} ms")
        if adcs:
            print(f"  adc read avg {statistics.mean(adcs):6.2f} ms   "
                  f"p95 {_pctl(adcs, 0.95):6.2f} ms   max {max(adcs):6.2f} ms")
        if misses:
            print(f"  ({misses} sample(s) dropped to timeouts)")
        print("\nIf the firmware were single-loop, every slow call would add "
              "seconds-long spikes here. With the RTOS task split, it doesn't.")


if __name__ == "__main__":
    main()
