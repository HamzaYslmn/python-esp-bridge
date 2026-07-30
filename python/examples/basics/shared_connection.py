"""Easy integration: one shared, thread-safe link with espbridge.connect().

A board's link can't be opened twice, and a Bridge is already safe to share
across threads. So instead of threading a Bridge object through your code, call
espbridge.connect() anywhere — every call with the same settings returns the
SAME auto-reconnecting connection.

    uv run basics/shared_connection.py

This runs several worker threads that all grab the shared link and hammer it
concurrently; their requests pipeline on the wire and stay correctly correlated.
"""
import threading
import time

import espbridge


def worker(n: int, stop: threading.Event) -> int:
    # No Bridge passed in — just ask for the shared one. Same link as everyone.
    esp = espbridge.connect(ble=False)
    reads = 0
    while not stop.is_set():
        esp.adc.read(34)          # read-only; safe to spam from many threads
        esp.ping()
        reads += 1
    print(f"[worker {n}] {reads} round-trips")
    return reads


def main() -> None:
    esp = espbridge.connect(ble=False)        # opens the link on first call
    print(f"connected to {esp.info.chip.name} ({esp.info.ident})")
    esp.adc.config(34, atten=11)

    stop = threading.Event()
    threads = [threading.Thread(target=worker, args=(i, stop)) for i in range(4)]
    for t in threads:
        t.start()
    time.sleep(3)
    stop.set()
    for t in threads:
        t.join()

    print("\nAll four threads shared ONE connection — no locks in your code, no\n"
          "second link. In a web app you'd call espbridge.connect() in each route;\n"
          "for asyncio: esp = espbridge.AsyncBridge.wrap(espbridge.connect(ble=False)).")
    espbridge.disconnect_all()                # close shared links at shutdown


if __name__ == "__main__":
    main()
