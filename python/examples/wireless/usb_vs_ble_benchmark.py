"""Measure link speed: latency + throughput, first over USB then Bluetooth.

    uv run benchmark.py                  # USB bench, then BLE bench
    uv run benchmark.py --usb-only / --ble-only
    uv run benchmark.py --target-baud 1500000   # try a faster serial link
    uv run benchmark.py --no-upgrade     # stay at 115200 for comparison

Speed knobs, in order of impact:
  1. Baud upgrade (on by default) — 115200 -> 1.5M/2M per chip (CP210x/CH340/
     CH9102, see constants.UPGRADE_BAUD; falls back to 921600 when the target
     fails). Native-USB chips (S2/S3/C3...) ignore baud: always USB speed.
  2. Pipelining — request() is thread-safe and the protocol allows 255
     requests in flight; issuing from several threads hides round-trip latency.
  3. Batching — one big payload (MAX_PAYLOAD=2048) beats many small ones.
  4. BLE link tuning — after auth the firmware negotiates a 15 ms connection
     interval + 251-byte LL packets, routes replies only to the requesting
     link, and lets ~3 BLE frames pipeline.
"""
import argparse
import statistics
import threading
import time

from espbridge import Bridge
from espbridge import constants as C

ECHO_SIZE = C.MAX_PAYLOAD  # 2048-byte payload per echo round trip


def bench_latency(esp: Bridge, n: int = 200) -> None:
    times = sorted(esp.ping() * 1000 for _ in range(n))
    print(f"latency ({n} pings, 4-byte payload):")
    print(f"  min {times[0]:.2f} ms   median {statistics.median(times):.2f} ms"
          f"   p95 {times[int(n * 0.95)]:.2f} ms")


def bench_throughput(esp: Bridge, seconds: float = 2.0, workers: int = 1) -> float:
    from espbridge import BridgeTimeoutError

    payload = bytes(i & 0xFF for i in range(ECHO_SIZE))
    done = failed = 0
    lock = threading.Lock()
    deadline = time.perf_counter() + seconds

    def worker() -> None:
        nonlocal done, failed
        while time.perf_counter() < deadline:
            try:
                # 3s is ~100x a worst-case BLE round trip; short enough that a
                # lost frame costs a brief stall + retry, not a wedged window.
                esp.request(C.SYS_PING, payload, timeout=3.0)
            except BridgeTimeoutError:
                with lock:
                    failed += 1
                return  # this lane is wedged; let the others finish
            with lock:
                done += 1

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0
    # Each echo moves the payload out AND back.
    mbps = done * ECHO_SIZE * 2 / elapsed / 1e6
    note = f"   ({failed} timed out)" if failed else ""
    print(f"throughput ({ECHO_SIZE}-byte echo x{workers} in flight): "
          f"{done / elapsed:6.1f} req/s = {mbps:.3f} MB/s round trip{note}")
    return mbps


def bench_bridge(esp: Bridge, ping_count: int = 200) -> None:
    info = esp.info
    ser = getattr(esp._t, "ser", None)
    if ser is not None:
        link = f"USB {ser.port} @ {ser.baudrate} baud"
        if C.Cap.NATIVE_USB in info.caps:
            link += " (native USB — baud is ignored)"
    else:
        link = f"Bluetooth {getattr(esp._t, 'address', '?')}"
    print(f"{info.ident}  {info.chip.name}  {link}\n")

    bench_latency(esp, n=ping_count)
    print()
    for workers in (1, 4, 8):
        bench_throughput(esp, workers=workers)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-p", "--port")
    ap.add_argument("--target-baud", type=int, help="upgrade to this baud (default: per-chip)")
    ap.add_argument("--no-upgrade", action="store_true", help="stay at 115200")
    ap.add_argument("--password", default="espbridge", help="Bluetooth link password")
    ap.add_argument("--usb-only", action="store_true")
    ap.add_argument("--ble-only", action="store_true")
    args = ap.parse_args()

    # USB first, then Bluetooth: same board, same firmware, two links.
    if not args.ble_only:
        print("=" * 24, "USB", "=" * 24)
        with Bridge(ble=False, port=args.port, upgrade_baud=not args.no_upgrade,
                    target_baud=args.target_baud) as esp:
            bench_bridge(esp)

    if not args.usb_only:
        print()
        print("=" * 22, "Bluetooth", "=" * 21)
        try:
            with Bridge(ble=True, password=args.password, timeout=10.0) as esp:
                # The firmware negotiates fast params post-auth; wait for the
                # central to grant them so numbers reflect steady state.
                time.sleep(1.5)
                bench_bridge(esp, ping_count=50)  # BLE pings are ~15x slower
        except Exception as e:
            print(f"Bluetooth bench skipped: {e}")


if __name__ == "__main__":
    main()
