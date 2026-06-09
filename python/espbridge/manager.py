"""Shared, thread-safe bridge connections — the easy way to integrate.

A serial/BLE link to a board can't be opened twice, and a :class:`Bridge` is
already safe to share across threads. So the right pattern for any larger
system — a web server, a threaded control app, an async service — is **one**
Bridge that everything shares. This module makes that a one-liner:

    import espbridge

    esp = espbridge.connect(ble=False)   # same live link, from anywhere
    esp.gpio.write(2, 1)                  # safe to call from any thread

Call ``connect()`` with the same settings from any module / thread / request
handler and you get back the same auto-(re)connecting link. Example — a Flask
or FastAPI route, where every request shares the one connection:

    @app.get("/gpio/{pin}")
    def read(pin: int):
        return {"level": espbridge.connect(ble=False).gpio.read(pin)}

For explicit lifetime control (own it, close it deterministically) construct a
:class:`BridgeManager` yourself. For ``await``, wrap the shared link with
``espbridge.AsyncBridge.wrap(espbridge.connect(...))``.
"""
from __future__ import annotations

import threading

from .bridge import Bridge


class BridgeManager:
    """Owns a single shared :class:`Bridge`, (re)connecting it on demand.

    Thread-safe: hand the *manager* to every thread / handler (or use the
    module-level :func:`connect`) instead of passing a raw Bridge around. The
    link is opened lazily on first use and transparently reopened if it drops.
    """

    def __init__(self, *, keepalive: float | None = None, **connect_kwargs):
        # Drop None values so they don't override Bridge's own defaults.
        self._kwargs = {k: v for k, v in connect_kwargs.items() if v is not None}
        self._bridge: Bridge | None = None
        self._lock = threading.RLock()
        # Optional heartbeat: ping the link every `keepalive` seconds so an idle
        # connection is kept warm and a silent drop is noticed (and reconnected)
        # promptly, instead of only on the next request. See _keepalive_loop.
        self._keepalive = keepalive
        self._ka_stop = threading.Event()
        self._ka_thread: threading.Thread | None = None
        if keepalive and keepalive > 0:
            self._ka_thread = threading.Thread(
                target=self._keepalive_loop, name="espbridge-keepalive", daemon=True)
            self._ka_thread.start()

    @property
    def connect_kwargs(self) -> dict:
        return dict(self._kwargs)

    @staticmethod
    def _stale(b: Bridge | None) -> bool:
        # Reconnect if there's no link, it's closing, or the board stopped
        # answering (is_alive() went False after a reset / brown-out / unplug).
        return b is None or not b.is_alive()

    def is_connected(self) -> bool:
        return not self._stale(self._bridge)

    def bridge(self) -> Bridge:
        """The live Bridge, connecting on first use and **reconnecting if the
        board reset / browned out / was unplugged** (see Bridge.is_alive).

        Fast path: a healthy link is returned without taking the lock (a single
        reference read is atomic under the GIL), so concurrent callers never
        serialize on a live connection — only on a (re)connect.
        """
        b = self._bridge
        if not self._stale(b):
            return b
        with self._lock:
            if self._stale(self._bridge):
                self._close_locked()
                self._bridge = Bridge(**self._kwargs)
            return self._bridge

    def connect(self, **overrides) -> Bridge:
        """(Re)connect, replacing any existing link. ``overrides`` win over the
        settings the manager was created with."""
        with self._lock:
            self._close_locked()
            kwargs = {**self._kwargs,
                      **{k: v for k, v in overrides.items() if v is not None}}
            self._bridge = Bridge(**kwargs)
            return self._bridge

    def _keepalive_loop(self) -> None:
        """Heartbeat: keep an *opened* link warm and self-heal a dropped one.

        Pings the live link each interval; if it has gone stale (board reset /
        brown-out / unplug) it reconnects. It deliberately does **not** open a
        link that was never used or was explicitly :meth:`disconnect`-ed (those
        leave ``_bridge`` None) — keepalive maintains a connection, it doesn't
        force one into existence.
        """
        while not self._ka_stop.wait(self._keepalive):
            b = self._bridge
            if b is None:
                continue                 # idle / disconnected — nothing to keep alive
            try:
                if self._stale(b):
                    self.bridge()        # dropped while we held it -> reconnect now
                else:
                    b.ping()             # warm the live link / detect a fresh drop
            except Exception:
                pass                     # next tick re-evaluates and retries

    def disconnect(self) -> None:
        """Close the link. A later :meth:`bridge`/:meth:`connect` reopens it
        (and the keepalive heartbeat, if any, keeps watching it)."""
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        """Tear down the current Bridge. Subclasses extend this to drop any
        per-connection state (mounted volumes, open ports) they layer on top."""
        if self._bridge is not None:
            try:
                self._bridge.close()
            except Exception:
                pass
            self._bridge = None

    def shutdown(self) -> None:
        """Stop the keepalive heartbeat (if running) and close the link. Use
        this for good when you're done with the manager; :meth:`disconnect` is
        the lighter, reopenable close."""
        self._ka_stop.set()
        self.disconnect()

    def __enter__(self) -> "BridgeManager":
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()


# ---- process-wide shared links -------------------------------------------------
# connect() hands out one manager per distinct set of settings, so the common
# espbridge.connect(ble=False) returns the same link everywhere in the process.
_shared: dict[tuple, BridgeManager] = {}
_shared_lock = threading.Lock()


def _key(kwargs: dict):
    try:
        return tuple(sorted(kwargs.items()))
    except TypeError:
        return None  # unhashable arg (e.g. transport=) -> never shared


def shared_manager(*, keepalive: float | None = None, **kwargs) -> BridgeManager:
    """The process-wide :class:`BridgeManager` for these settings, created once
    and reused. Unhashable settings (e.g. ``transport=``) get a fresh manager.

    ``keepalive`` (seconds) only takes effect when the manager is first created
    for a given set of settings; it is not part of the sharing key."""
    key = _key(kwargs)
    if key is None:
        return BridgeManager(keepalive=keepalive, **kwargs)
    with _shared_lock:
        mgr = _shared.get(key)
        if mgr is None:
            mgr = BridgeManager(keepalive=keepalive, **kwargs)
            _shared[key] = mgr
        return mgr


def connect(*, keepalive: float | None = None, **kwargs) -> Bridge:
    """Return a process-wide shared, thread-safe :class:`Bridge` for these
    settings, connecting on first use.

    Same settings -> same live link, so any thread / handler can call this and
    share one connection (a board's link can't be opened twice). Accepts the
    same keyword arguments as ``Bridge`` (``port=``, ``ble=``, ``name=``, ...).

    Pass ``keepalive=<seconds>`` to run a background heartbeat that keeps the
    link warm and transparently reconnects it if the board resets / drops.
    """
    return shared_manager(keepalive=keepalive, **kwargs).bridge()


def disconnect_all() -> None:
    """Close every shared link opened via :func:`connect` (e.g. at shutdown)."""
    with _shared_lock:
        managers = list(_shared.values())
        _shared.clear()
    for mgr in managers:
        mgr.shutdown()
