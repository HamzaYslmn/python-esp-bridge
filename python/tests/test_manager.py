"""Shared connection layer: BridgeManager + espbridge.connect()."""
from espbridge import BridgeManager, connect, disconnect_all, shared_manager
from espbridge import manager as _mgr


def test_manager_caches_and_disconnects(fw):
    mgr = BridgeManager(transport=fw.transport, upgrade_baud=False, reset_on_open=False)
    b1 = mgr.bridge()
    b2 = mgr.bridge()
    assert b1 is b2                       # same live link returned each call
    assert mgr.is_connected()
    mgr.disconnect()
    assert not mgr.is_connected()


def test_manager_context_manager_closes(fw):
    with BridgeManager(transport=fw.transport, upgrade_baud=False,
                       reset_on_open=False) as mgr:
        assert mgr.bridge().info is not None
    assert not mgr.is_connected()


def test_shared_manager_keyed_by_settings():
    try:
        m1 = shared_manager(port="COM-test-A")
        m2 = shared_manager(port="COM-test-A")
        m3 = shared_manager(port="COM-test-B")
        assert m1 is m2                  # same settings -> same manager
        assert m3 is not m1
    finally:
        disconnect_all()


class FakeBridge:
    def __init__(self, **kw):
        self.kw = kw
        self.alive = True
        self.pings = 0

    def is_closing(self):
        return not self.alive

    def is_alive(self):
        return self.alive

    def ping(self, *a, **k):
        if not self.alive:
            raise RuntimeError("link down")
        self.pings += 1

    def close(self):
        self.alive = False


def test_connect_shares_one_link(monkeypatch):
    built = []
    monkeypatch.setattr(_mgr, "Bridge", lambda **kw: built.append(FakeBridge(**kw)) or built[-1])
    try:
        a = connect(port="COM-shared")
        b = connect(port="COM-shared")
        assert a is b                    # one shared link...
        assert len(built) == 1           # ...connected exactly once
    finally:
        disconnect_all()


def test_manager_reconnects_after_board_reset(monkeypatch):
    built = []
    monkeypatch.setattr(_mgr, "Bridge", lambda **kw: built.append(FakeBridge(**kw)) or built[-1])
    mgr = _mgr.BridgeManager(port="COM-heal")
    b1 = mgr.bridge()
    assert mgr.bridge() is b1            # healthy link is reused
    b1.alive = False                     # simulate a board reset / brown-out
    b2 = mgr.bridge()
    assert b2 is not b1                  # dead link -> transparently reconnected
    assert len(built) == 2 and b2.alive


def _wait(pred, timeout=2.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_keepalive_warms_live_link_and_reconnects_drop(monkeypatch):
    built = []
    monkeypatch.setattr(_mgr, "Bridge", lambda **kw: built.append(FakeBridge(**kw)) or built[-1])
    mgr = _mgr.BridgeManager(port="COM-ka", keepalive=0.02)
    try:
        b1 = mgr.bridge()                       # open the link
        assert _wait(lambda: b1.pings >= 2)     # heartbeat warms the live link
        b1.alive = False                        # board drops out
        assert _wait(lambda: len(built) == 2)   # heartbeat reconnects on its own
        assert mgr.bridge() is built[1] and built[1].alive
    finally:
        mgr.shutdown()


def test_keepalive_does_not_open_idle_or_disconnected_link(monkeypatch):
    built = []
    monkeypatch.setattr(_mgr, "Bridge", lambda **kw: built.append(FakeBridge(**kw)) or built[-1])
    mgr = _mgr.BridgeManager(port="COM-ka2", keepalive=0.02)
    try:
        import time
        time.sleep(0.1)
        assert built == []                      # never used -> heartbeat opens nothing
        mgr.bridge()
        mgr.disconnect()                         # explicit close
        time.sleep(0.1)
        assert not mgr.is_connected()            # heartbeat does not resurrect it
    finally:
        mgr.shutdown()


def test_shutdown_stops_heartbeat(monkeypatch):
    built = []
    monkeypatch.setattr(_mgr, "Bridge", lambda **kw: built.append(FakeBridge(**kw)) or built[-1])
    mgr = _mgr.BridgeManager(port="COM-ka3", keepalive=0.02)
    b1 = mgr.bridge()
    assert _wait(lambda: b1.pings >= 1)
    mgr.shutdown()
    assert mgr._ka_thread is not None
    assert _wait(lambda: not mgr._ka_thread.is_alive())
