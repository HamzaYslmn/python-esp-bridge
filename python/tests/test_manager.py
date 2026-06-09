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


def test_connect_shares_one_link(monkeypatch):
    built = []

    class FakeBridge:
        def __init__(self, **kw):
            built.append(kw)

        def is_closing(self):
            return False

        def close(self):
            pass

    monkeypatch.setattr(_mgr, "Bridge", FakeBridge)
    try:
        a = connect(port="COM-shared")
        b = connect(port="COM-shared")
        assert a is b                    # one shared link...
        assert len(built) == 1           # ...connected exactly once
    finally:
        disconnect_all()
