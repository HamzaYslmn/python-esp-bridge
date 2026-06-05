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
    main:     latency-samples ping+ADC    (bridge_rx task)
"""
import statistics
import sys
import threading
import time

from espbridge import Bridge

SSID = sys.argv[1] if len(sys.argv) > 1 else ""
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else ""

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
            except Exception as e:
                print(f"[net] {e}")
            stop.wait(1.0)
    else:
        print("[net] no SSID given — using Wi-Fi scans as the slow workload")
        while not stop.is_set():
            nets = esp.wifi.scan()           # several seconds inside bridge_net
            print(f"[net] scan finished: {len(nets)} networks")


def breathing_led(esp: Bridge, stop: threading.Event) -> None:
    """Smooth PWM fade — handled by bridge_rx, immune to network stalls."""
    esp.pwm.attach(LED, freq=1000, resolution_bits=10)
    while not stop.is_set():
        for pct in list(range(0, 101, 4)) + list(range(100, -1, -4)):
            esp.pwm.duty_pct(LED, pct)
            time.sleep(0.02)
    esp.pwm.detach(LED)


def main() -> None:
    with Bridge() as esp:
        print(f"connected to {esp.info.chip.name} ({esp.info.name or esp.info.mac})\n")
        esp.adc.config(ADC_PIN, atten=11)

        stop = threading.Event()
        threads = [
            threading.Thread(target=slow_network_work, args=(esp, stop), daemon=True),
            threading.Thread(target=breathing_led, args=(esp, stop), daemon=True),
        ]
        for t in threads:
            t.start()

        # Sample fast-lane latency WHILE the network task is blocking.
        pings, adcs = [], []
        t_end = time.monotonic() + 15
        while time.monotonic() < t_end:
            pings.append(esp.ping() * 1000)
            t0 = time.perf_counter()
            esp.adc.read(ADC_PIN)
            adcs.append((time.perf_counter() - t0) * 1000)
            time.sleep(0.05)

        stop.set()
        for t in threads:
            t.join(timeout=5)

        print(f"\nfast lane while Wi-Fi/TCP was busy ({len(pings)} samples):")
        print(f"  ping     avg {statistics.mean(pings):5.2f} ms   "
              f"p95 {sorted(pings)[int(len(pings) * 0.95)]:5.2f} ms   "
              f"max {max(pings):5.2f} ms")
        print(f"  adc read avg {statistics.mean(adcs):5.2f} ms   "
              f"p95 {sorted(adcs)[int(len(adcs) * 0.95)]:5.2f} ms   "
              f"max {max(adcs):5.2f} ms")
        print("\nIf the firmware were single-loop, every TCP connect would add "
              "seconds-long spikes here. With the RTOS task split, it doesn't.")


if __name__ == "__main__":
    main()
