"""Measure link speed: round-trip latency and echo throughput.

    uv run benchmark.py                  # auto-detect, auto baud upgrade
    uv run benchmark.py --target-baud 1500000   # try a faster link
    uv run benchmark.py --no-upgrade     # stay at 115200 for comparison

Speed knobs, in order of impact:
  1. Baud upgrade (on by default) — 115200 -> 921600+ on CP210x/CH340/CH9102.
     Native-USB chips (S2/S3/C3...) ignore baud: the link is always USB speed.
  2. Pipelining — request() is thread-safe and the protocol allows 255
     requests in flight; issuing from several threads hides round-trip latency.
  3. Batching — one big payload (MAX_PAYLOAD=2048) beats many small ones.
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
    payload = bytes(i & 0xFF for i in range(ECHO_SIZE))
    done = 0
    lock = threading.Lock()
    deadline = time.perf_counter() + seconds

    def worker() -> None:
        nonlocal done
        while time.perf_counter() < deadline:
            esp.request(C.SYS_PING, payload, timeout=5.0)
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
    print(f"throughput ({ECHO_SIZE}-byte echo x{workers} in flight): "
          f"{done / elapsed:6.1f} req/s = {mbps:.3f} MB/s round trip")
    return mbps


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-p", "--port")
    ap.add_argument("--target-baud", type=int, help="upgrade to this baud (default: per-chip)")
    ap.add_argument("--no-upgrade", action="store_true", help="stay at 115200")
    args = ap.parse_args()

    with Bridge(args.port, upgrade_baud=not args.no_upgrade,
                target_baud=args.target_baud) as esp:
        info = esp.info
        ser = getattr(esp._t, "ser", None)
        link = f"{ser.port} @ {ser.baudrate} baud" if ser else "custom transport"
        if C.Cap.NATIVE_USB in info.caps:
            link += " (native USB — baud is ignored)"
        print(f"{info.name or '(unnamed)'}  {info.chip.name}  {link}\n")

        bench_latency(esp)
        print()
        for workers in (1, 4, 8):
            bench_throughput(esp, workers=workers)


if __name__ == "__main__":
    main()
