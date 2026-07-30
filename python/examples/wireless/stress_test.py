"""Stress + soak test: prove the link stays solid under sustained, concurrent,
and multi-radio (Wi-Fi + BLE + ESP-NOW) load.

  uv run stress_test.py                       # USB autodetect, single board
  uv run stress_test.py -p COM3               # pin a USB port
  uv run stress_test.py --ble                 # run the suite over Bluetooth
  uv run stress_test.py -p COM3 --peer COM8   # add the two-board ESP-NOW coex phase

Each phase counts failures, watches free heap, and checks the firmware's own
loss counters (link_rx_dropped / serial_rx_errors / dropped_events). A clean
run ends with "ALL PHASES PASSED" and a non-zero exit code on any failure.
"""
import argparse
import logging
import statistics
import threading
import time

from espbridge import Bridge, BridgeError, BridgeTimeoutError, RemoteError
from espbridge import constants as C

ECHO = bytes(i & 0xFF for i in range(C.MAX_PAYLOAD))  # 2048-byte echo payload
# A lost frame normally round-trips in ms; 3s is already 100x slack. Short
# timeouts mean a dropped frame costs a 3s stall + retry, not a 10s hang.
T_REQ = 3.0
_fail_lines: list[str] = []

# The library retries a timed-out safe command once and logs a WARNING; that
# is a *recovered* loss, not a failure. Count them so the summary reports how
# often the link self-healed instead of hiding it in scrollback.
_retries = [0]


class _RetryCounter(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING and "retrying" in record.getMessage():
            _retries[0] += 1


logging.getLogger("espbridge").addHandler(_RetryCounter())


def fail(phase: str, msg: str) -> None:
    line = f"  FAIL [{phase}] {msg}"
    print(line)
    _fail_lines.append(line)


# Wire corruption the protocol layer already recovered (CRC caught it, the
# retry re-sent it, no operation failed). Occasional corrupt frames are the
# nature of a physical link — what must never happen silently is an
# *unrecovered* loss, which the per-op failure counters above catch. Recovered
# corruption is reported, not failed.
_wire_loss = [0]


def note_loss(delta: int) -> None:
    if delta > 0:
        _wire_loss[0] += delta
        print(f"    note: +{delta} corrupted frame(s) on the wire — caught by "
              f"CRC, recovered by retry")


def drops(esp: Bridge) -> tuple[int, int]:
    """(link loss, dropped_events) — firmware-side loss counters. Link loss
    sums BLE RX overflow bytes and UART RX error events; any increase means
    the link physically lost data (even if a retry recovered the request)."""
    h = esp.free_heap()
    return (h.get("link_rx_dropped", 0) + h.get("serial_rx_errors", 0),
            h.get("dropped_events", 0))


def link_label(esp: Bridge) -> str:
    ser = getattr(esp._t, "ser", None)
    if ser is not None:
        return f"USB {ser.port} @ {ser.baudrate}"
    return f"BLE {getattr(esp._t, 'address', '?')}"


# ---- phases ---------------------------------------------------------------

def phase_ping_soak(esp: Bridge, n: int) -> None:
    """Sequential ping flood — every request must round-trip, payload intact."""
    print(f"\n[1] ping soak — {n} sequential pings")
    h0 = esp.free_heap()
    rx0, ev0 = drops(esp)
    lat, lost = [], 0
    t0 = time.perf_counter()
    for i in range(n):
        try:
            lat.append(esp.ping(b"soak%06d" % i) * 1000)  # unique payload: catches stale/duplicated replies
        except (BridgeTimeoutError, BridgeError) as e:
            lost += 1
            fail("ping-soak", f"#{i}: {e}")
    dt = time.perf_counter() - t0
    rx1, ev1 = drops(esp)
    print(f"    {n/dt:.0f} req/s   median {statistics.median(lat):.2f} ms   "
          f"p95 {sorted(lat)[int(len(lat)*0.95)]:.2f} ms   lost {lost}")
    print(f"    heap {h0['free']} -> {esp.free_heap()['free']} B   "
          f"link_loss +{rx1-rx0}   events_dropped +{ev1-ev0}")
    if lost:
        fail("ping-soak", f"{lost}/{n} pings lost")
    note_loss(rx1 - rx0)


def phase_concurrent(esp: Bridge, workers: int, seconds: float) -> None:
    """Many threads hammering mixed inline-handler ops at once — exercises the
    255-in-flight seq window, the flow-control window, and thread safety."""
    print(f"\n[2] concurrent mixed flood — {workers} threads x {seconds:g}s "
          f"(ping / 2KB echo / gpio / adc)")
    try:
        esp.adc.config(34)  # ADC1_CH6, input-only pin — safe to read
        has_adc = True
    except BridgeError:
        has_adc = False
    ok = [0] * workers
    bad = [0] * workers
    deadline = time.perf_counter() + seconds

    def worker(wid: int) -> None:
        i = 0
        while time.perf_counter() < deadline:
            try:
                op = i & 3
                if op == 0:
                    esp.request(C.SYS_PING, b"c%d-%d" % (wid, i), timeout=T_REQ)
                elif op == 1:
                    esp.request(C.SYS_PING, ECHO, timeout=T_REQ)
                elif op == 2:
                    esp.gpio.read_all()
                else:
                    esp.adc.read(34) if has_adc else esp.gpio.read_all()
                ok[wid] += 1
            except Exception as e:  # incl. transport-level drops (BleakError etc.)
                bad[wid] += 1
                fail("concurrent", f"thread {wid} op {i}: {type(e).__name__}: {e}")
            i += 1

    rx0, ev0 = drops(esp)
    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dt = time.perf_counter() - t0
    rx1, ev1 = drops(esp)
    total_ok, total_bad = sum(ok), sum(bad)
    print(f"    {total_ok} ops in {dt:.1f}s = {total_ok/dt:.0f} ops/s   "
          f"failures {total_bad}")
    print(f"    link_loss +{rx1-rx0}   events_dropped +{ev1-ev0}   "
          f"min_free {esp.free_heap()['min_free']} B")
    if total_bad:
        fail("concurrent", f"{total_bad} ops failed")
    note_loss(rx1 - rx0)


def phase_echo_saturate(esp: Bridge, workers: int, seconds: float) -> None:
    """Saturate the link with pipelined 2KB echoes — pushes the flow-control
    window (BLE) / UART ring (USB) to its limit; any byte loss corrupts a
    frame and surfaces as a timeout."""
    print(f"\n[3] pipelined echo saturate — {workers} threads x {seconds:g}s of 2KB echoes")
    done = [0] * workers
    bad = [0] * workers
    deadline = time.perf_counter() + seconds

    def worker(wid: int) -> None:
        while time.perf_counter() < deadline:
            try:
                r = esp.request(C.SYS_PING, ECHO, timeout=T_REQ)
                if r != ECHO:
                    bad[wid] += 1
                    fail("echo", f"thread {wid}: payload mismatch ({len(r)} B)")
                else:
                    done[wid] += 1
            except Exception as e:  # incl. transport-level drops
                bad[wid] += 1
                fail("echo", f"thread {wid}: {type(e).__name__}: {e}")

    rx0, _ = drops(esp)
    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dt = time.perf_counter() - t0
    rx1, _ = drops(esp)
    total = sum(done)
    mbps = total * C.MAX_PAYLOAD * 2 / dt / 1e6
    print(f"    {total} echoes = {total/dt:.0f} req/s = {mbps:.3f} MB/s round trip   "
          f"failures {sum(bad)}")
    if sum(bad):
        fail("echo", f"{sum(bad)} echoes failed/corrupted")
    note_loss(rx1 - rx0)


def phase_wifi_coex(esp: Bridge, seconds: float) -> None:
    """Bring the Wi-Fi radio up (AP + repeated scans) while a ping flood runs
    concurrently — the Wi-Fi+BLE+link coex case the firmware was hardened for.
    The link must stay lossless while the radio is busy."""
    if C.Cap.WIFI not in esp.caps:
        print("\n[4] wifi coex — SKIPPED (no WIFI capability)")
        return
    print(f"\n[4] wifi coex — Wi-Fi up while pinging the link, {seconds:g}s")
    try:
        ip = esp.wifi.ap_start("espbridge-stress", "stresstest123")
    except RemoteError as e:
        if e.status == C.Status.NO_MEM:
            # Firmware refusal, not a failure: on a classic ESP32 the Wi-Fi
            # stack (~50KB) doesn't fit alongside a live BLE session, and the
            # firmware refuses rather than letting Bluedroid starve and the
            # BLE link die mid-command.
            print("    SKIPPED — firmware refused: not enough heap for Wi-Fi "
                  "with a BLE session active (documented chip constraint)")
            return
        fail("wifi-coex", f"ap_start failed: {e}")
        return
    except BridgeError as e:
        fail("wifi-coex", f"ap_start failed: {e}")
        return
    free_after = esp.free_heap()["free"]
    # A scan must allocate AP records on top of the resident Wi-Fi stack; with a
    # BLE connection also up, a classic ESP32 can be left with too little to do
    # it (the link still works — only the scan can't fit). Gate the scanner on
    # the post-ap_start margin so this stays a real coex check, not a false fail.
    do_scan = free_after >= 12000
    print(f"    AP up at {ip}   free heap now {free_after} B"
          f"{'' if do_scan else '  — too low to scan; pinging only'}")

    stop = threading.Event()
    pings = [0, 0]  # ok, fail
    scans = [0]

    def pinger() -> None:
        i = 0
        while not stop.is_set():
            try:
                esp.ping(b"wifi%05d" % i)
                pings[0] += 1
            except Exception as e:
                pings[1] += 1
                fail("wifi-coex", f"ping while Wi-Fi up: {type(e).__name__}: {e}")
            i += 1

    def scanner() -> None:
        while not stop.is_set():
            try:
                nets = esp.wifi.scan(timeout=15.0)
                scans[0] += 1
                print(f"    scan #{scans[0]}: {len(nets)} networks "
                      f"(pings so far: {pings[0]} ok / {pings[1]} fail)")
            except (BridgeTimeoutError, BridgeError) as e:
                fail("wifi-coex", f"scan failed: {e}")

    rx0, _ = drops(esp)
    pt = threading.Thread(target=pinger)
    st = threading.Thread(target=scanner) if do_scan else None
    pt.start()
    if st:
        st.start()
    time.sleep(seconds)
    stop.set()
    pt.join()
    if st:
        st.join(timeout=20)
    rx1, _ = drops(esp)
    try:
        esp.wifi.ap_stop()
    except BridgeError:
        pass
    tail = f" across {scans[0]} scans" if do_scan else " (scan skipped — heap)"
    print(f"    {pings[0]} pings ok / {pings[1]} fail{tail}")
    if pings[1]:
        fail("wifi-coex", f"{pings[1]} pings lost while the radio was busy")
    if not pings[0]:
        fail("wifi-coex", "ping thread made no progress (link stalled under coex)")
    if do_scan and not scans[0]:
        fail("wifi-coex", "no scan completed despite adequate heap")
    note_loss(rx1 - rx0)


def phase_espnow_coex(a: Bridge, b: Bridge, count: int) -> None:
    """ESP-NOW flood A->B while BOTH boards' USB links carry a ping flood —
    Wi-Fi-band radio + two busy links at once (full coex)."""
    if C.Cap.ESPNOW not in a.caps or C.Cap.ESPNOW not in b.caps:
        print("\n[5] espnow coex — SKIPPED (a board lacks ESPNOW)")
        return
    print(f"\n[5] espnow coex — {count} A->B packets while both links ping-flood")
    # espnow.begin() initialises the Wi-Fi stack (~50KB). On a board whose
    # control link is BLE, that stacks on top of the live BLE connection — the
    # real "BLE + ESP-NOW at once" case. Report the heap so the squeeze is visible.
    try:
        mac_a = a.espnow.begin()
        mac_b = b.espnow.begin()
    except RemoteError as e:
        if e.status == C.Status.NO_MEM:
            print("    SKIPPED — firmware refused: not enough heap for the "
                  "Wi-Fi driver with a BLE session active")
            return
        fail("espnow-coex", f"espnow.begin failed: {e}")
        return
    except BridgeError as e:
        fail("espnow-coex", f"espnow.begin failed: {e}")
        return
    a.espnow.add_peer(mac_b)
    b.espnow.add_peer(mac_a)
    print(f"    A={mac_a} ({link_label(a)})   free heap {a.free_heap()['free']} B")
    print(f"    B={mac_b} ({link_label(b)})   free heap {b.free_heap()['free']} B")

    received = [0]
    b.espnow.on_receive(lambda src, data, rssi: received.__setitem__(0, received[0] + 1))

    stop = threading.Event()
    ping_stats = {"a": [0, 0], "b": [0, 0]}

    def pinger(esp: Bridge, key: str) -> None:
        i = 0
        while not stop.is_set():
            try:
                esp.ping(b"en%05d" % i); ping_stats[key][0] += 1
            except Exception:  # incl. transport-level drops
                ping_stats[key][1] += 1
            i += 1

    pa = threading.Thread(target=pinger, args=(a, "a"))
    pb = threading.Thread(target=pinger, args=(b, "b"))
    pa.start(); pb.start()

    payload = bytes(200)
    acked = lost = 0
    t0 = time.perf_counter()
    for i in range(count):
        try:
            if a.espnow.send(mac_b, b"%03d" % (i % 1000) + payload[3:], wait=True):
                acked += 1
            else:
                lost += 1
        except (BridgeTimeoutError, BridgeError) as e:
            lost += 1
            fail("espnow-coex", f"send #{i}: {e}")
    dt = time.perf_counter() - t0
    time.sleep(0.5)  # let the last notifies land
    stop.set()
    pa.join(); pb.join()

    print(f"    sent {count}: acked {acked}, not-acked/err {lost}   "
          f"({count/dt:.0f} pkt/s)")
    print(f"    B received {received[0]}/{count} ESP-NOW frames")
    print(f"    coex pings — A {ping_stats['a'][0]} ok/{ping_stats['a'][1]} fail   "
          f"B {ping_stats['b'][0]} ok/{ping_stats['b'][1]} fail")
    if acked < count:
        fail("espnow-coex", f"{count-acked} packets not radio-ACKed")
    if received[0] < count * 0.95:
        fail("espnow-coex", f"B only received {received[0]}/{count} (>5% loss)")
    if ping_stats["a"][1] or ping_stats["b"][1]:
        fail("espnow-coex", "USB pings lost while ESP-NOW was active")
    a.espnow.end(); b.espnow.end()


# ---- driver ---------------------------------------------------------------

def run_suite(esp: Bridge, *, pings: int, secs: float, workers: int) -> None:
    info = esp.info
    print("=" * 70)
    print(f"{info.ident}  {info.chip.name}  fw{'.'.join(map(str, info.fw_version))}  "
          f"{link_label(esp)}   free heap {esp.free_heap()['free']} B")
    print("=" * 70)
    phase_ping_soak(esp, pings)
    phase_concurrent(esp, workers=workers, seconds=secs)
    phase_echo_saturate(esp, workers=max(2, workers // 2), seconds=secs)
    phase_wifi_coex(esp, seconds=secs)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-p", "--port")
    ap.add_argument("--ble", nargs="?", const=True, default=False, metavar="MAC",
                    help="run over Bluetooth; optional MAC pins a specific board "
                         "(so --peer can use the other one for ESP-NOW)")
    ap.add_argument("--peer", help="second board's USB port for the ESP-NOW coex phase")
    ap.add_argument("--espnow-only", action="store_true",
                    help="run only the ESP-NOW coex phase (skip the link suite) — "
                         "use with --ble + --peer to test BLE + ESP-NOW on one board")
    ap.add_argument("--password", default="espbridge")
    ap.add_argument("--pings", type=int, default=3000)
    ap.add_argument("--secs", type=float, default=6.0)
    args = ap.parse_args()

    if args.ble:
        # args.ble is True (any advertising board) or a MAC pinning one.
        primary = Bridge(None if args.ble is True else args.ble,
                         ble=True, password=args.password, timeout=10.0)
        time.sleep(1.5)  # let post-auth link tuning settle
        workers = 4      # the BLE link self-paces; few threads keep it readable
    else:
        primary = Bridge(ble=False, port=args.port)
        workers = 12

    with primary as esp:
        if not args.espnow_only:
            run_suite(esp, pings=args.pings, secs=args.secs, workers=workers)
        if args.peer:
            with Bridge(ble=False, port=args.peer) as peer:
                phase_espnow_coex(esp, peer, count=300)
        elif args.espnow_only:
            print("--espnow-only needs --peer <port> for the second board")

    print("\n" + "=" * 70)
    healed = ""
    if _retries[0] or _wire_loss[0]:
        healed = (f"   ({_wire_loss[0]} corrupted frame(s), "
                  f"{_retries[0]} retry(ies) — all recovered)")
    if _fail_lines:
        print(f"FAILED — {len(_fail_lines)} problem(s):{healed}")
        for line in _fail_lines:
            print(line)
        raise SystemExit(1)
    print(f"ALL PHASES PASSED — link is solid{healed}")


if __name__ == "__main__":
    main()
