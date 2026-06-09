"""Bridge under heavy multi-threaded use: flow-control accounting, the
sub-API creation race, and many threads sharing one connection."""
import threading


def test_ack_releases_only_its_own_bytes(bridge):
    """Regression: a reply must release only that request's reserved bytes, not
    reset the whole window to zero (which erased other in-flight requests')."""
    b = bridge
    b._unacked = b._send_bytes = 0
    b._reserve_window(100)
    b._reserve_window(150)
    b._reserve_window(100)
    assert b._unacked == 350                 # three requests in flight
    b._ack_window(100)
    assert b._unacked == 250                 # only the replied 100 released
    b._ack_window(150)
    b._ack_window(100)
    assert b._unacked == 0


def test_reply_clears_pending_send_bytes(bridge):
    """A request reply proves earlier fire-and-forget send() bytes were consumed."""
    b = bridge
    b._unacked = 0
    b._send_bytes = 500
    b._reserve_window(80)
    b._ack_window(80)
    assert b._unacked == 0
    assert b._send_bytes == 0


def test_concurrent_first_subapi_access_yields_one_instance(bridge):
    """Many threads first-touching esp.gpio at once must share one sub-API
    object (a second would leak with a duplicate event registration)."""
    start = threading.Barrier(8)
    seen = []

    def grab():
        start.wait()
        seen.append(bridge.gpio)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(g is seen[0] for g in seen)
    assert bridge.gpio is seen[0]            # and the cached one is that same object


def test_many_threads_share_one_bridge(bridge):
    """Several threads issuing requests on one shared Bridge: all succeed,
    correlated correctly, no exceptions."""
    bridge.gpio.mode(2, "output")
    bridge.gpio.write(2, 1)
    errors = []

    def hammer():
        try:
            for _ in range(25):
                assert bridge.gpio.read(2) == 1
                assert bridge.ping() >= 0
        except Exception as e:  # noqa: BLE001 — surface any thread's failure
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert bridge._unacked == 0              # accounting balanced after the storm
