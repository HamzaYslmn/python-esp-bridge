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

    def __init__(self, **connect_kwargs):
        # Drop None values so they don't override Bridge's own defaults.
        self._kwargs = {k: v for k, v in connect_kwargs.items() if v is not None}
        self._bridge: Bridge | None = None
        self._lock = threading.RLock()

    @property
    def connect_kwargs(self) -> dict:
        return dict(self._kwargs)

    @staticmethod
    def _stale(b: Bridge | None) -> bool:
        return b is None or b.is_closing()

    def is_connected(self) -> bool:
        return not self._stale(self._bridge)

    def bridge(self) -> Bridge:
        """The live Bridge, auto-connecting with the configured settings.

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

    def disconnect(self) -> None:
        """Close the link. A later :meth:`bridge`/:meth:`connect` reopens it."""
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

    def __enter__(self) -> "BridgeManager":
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()


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


def shared_manager(**kwargs) -> BridgeManager:
    """The process-wide :class:`BridgeManager` for these settings, created once
    and reused. Unhashable settings (e.g. ``transport=``) get a fresh manager."""
    key = _key(kwargs)
    if key is None:
        return BridgeManager(**kwargs)
    with _shared_lock:
        mgr = _shared.get(key)
        if mgr is None:
            mgr = BridgeManager(**kwargs)
            _shared[key] = mgr
        return mgr


def connect(**kwargs) -> Bridge:
    """Return a process-wide shared, thread-safe :class:`Bridge` for these
    settings, connecting on first use.

    Same settings -> same live link, so any thread / handler can call this and
    share one connection (a board's link can't be opened twice). Accepts the
    same keyword arguments as ``Bridge`` (``port=``, ``ble=``, ``name=``, ...).
    """
    return shared_manager(**kwargs).bridge()


def disconnect_all() -> None:
    """Close every shared link opened via :func:`connect` (e.g. at shutdown)."""
    with _shared_lock:
        managers = list(_shared.values())
        _shared.clear()
    for mgr in managers:
        mgr.disconnect()
