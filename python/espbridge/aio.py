"""asyncio facade: ``await`` every bridge call and fan out concurrent I/O.

The ESP32 firmware is *already* concurrent — requests carry a sequence number,
many can be in flight at once, and a slow call on one task never stalls the
fast lane (see ``examples/basics/rtos_concurrency.py``). ``AsyncBridge`` simply
exposes that concurrency to ``asyncio`` so you can ``await`` bridge calls and
run them together with ``asyncio.gather``, instead of juggling threads yourself:

    import asyncio
    from espbridge import AsyncBridge

    async def main():
        async with AsyncBridge(ble=False) as esp:
            await esp.gpio.mode(2, "output")
            await esp.gpio.write(2, 1)
            # both reads are issued together and overlap on the wire:
            t, h = await asyncio.gather(esp.adc.read(34), esp.adc.read(35))
            print(t, h)

    asyncio.run(main())

Edge interrupts (``gpio.watch``) become an async stream — no callback thread:

    async with AsyncBridge() as esp:
        await esp.gpio.mode(4, "input_pullup")
        await esp.gpio.watch(4, edge="falling", debounce_ms=30)
        async for ev in esp.gpio_events():
            print("pressed", ev.pin, ev.millis)

How it works (and what to expect): the firmware speaks a synchronous
request/response protocol and the underlying ``Bridge`` is fully thread-safe,
so each awaited call runs the matching ``Bridge`` method in a worker thread
(``asyncio.to_thread``). Calls awaited together genuinely overlap on the link
because ``Bridge`` pipelines concurrent requests and correlates replies by
sequence number — ``await`` here is about *the host not blocking*, not about
moving work onto the ESP32 (the firmware's task split already does that).

For tight inner loops or fire-and-forget streaming (e.g. pushing OLED frames),
the plain synchronous ``Bridge`` is still the right tool — there is nothing to
gain from awaiting each byte, and a thread per call would only add overhead.
"""
from __future__ import annotations

import asyncio
import functools

from . import constants as C
from .bridge import Bridge
from .gpio import EdgeEvent
from .watch import WatchEvent


def _to_coro(fn):
    """Wrap a blocking bridge callable so it runs in a thread and can be awaited."""
    @functools.wraps(fn)
    async def _call(*args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)
    return _call


def _async_attr(attr):
    """Adapt a bound bridge attribute for async use: callables become coroutines
    that run in a worker thread; plain values (info, caps, max_write, ...) pass
    through. One uniform rule — nothing has to enumerate which calls touch the
    wire, so a new Bridge method is awaitable automatically."""
    return _to_coro(attr) if callable(attr) else attr


class _AsyncSubApi:
    """Proxies a sync sub-API (``esp.gpio``, ``esp.adc``, ...): every method
    becomes awaitable; plain attributes (e.g. ``i2c.max_write``) pass through."""

    def __init__(self, target):
        self._target = target

    def __getattr__(self, name):
        if name.startswith("_"):  # don't expose the sub-API's internals
            raise AttributeError(name)
        return _async_attr(getattr(self._target, name))

    def __dir__(self):
        return dir(self._target)


class _EventStream:
    """Async iterator over a firmware event, fed from the reader thread.

    Registered with ``Bridge.on_event``; each event payload is handed to the
    asyncio loop via ``call_soon_threadsafe`` and surfaces through ``async for``.
    """

    def __init__(self, bridge: Bridge, cmd: int, loop, parse=None, maxsize: int = 0):
        self._bridge = bridge
        self._cmd = cmd
        self._loop = loop
        self._parse = parse
        self._queue: asyncio.Queue = asyncio.Queue(maxsize)
        bridge.on_event(cmd, self._on_event)

    def _on_event(self, payload: bytes) -> None:
        item = self._parse(payload) if self._parse else payload
        if item is None:  # parser rejected a malformed/short payload
            return
        # Hop from the reader thread onto the asyncio loop; drop if the queue is
        # full (a bounded maxsize gives back-pressure without blocking the reader).
        self._loop.call_soon_threadsafe(self._offer, item)

    def _offer(self, item) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._queue.get()

    async def aclose(self) -> None:
        """Stop the stream and unregister it from the bridge (call when done, or
        just break out of the ``async for`` and let it be garbage-collected)."""
        self._bridge.off_event(self._cmd, self._on_event)


class AsyncBridge:
    """``asyncio`` wrapper around :class:`Bridge`.

    Accepts the same arguments as ``Bridge``. Connection happens off the event
    loop, so use it as an async context manager (or ``await .connect()``):

    >>> async with AsyncBridge(ble=False) as esp:
    ...     await esp.gpio.write(2, 1)

    The wrapped synchronous bridge is available as ``.bridge`` for anything not
    surfaced here (events, ``on_event``, device drivers).
    """

    def __init__(self, **kwargs):
        # Bridge() connects (and blocks) in its constructor, so defer it.
        self._kwargs = kwargs
        self._bridge = None
        self._owned = True  # we opened it, so we close it

    @classmethod
    def wrap(cls, bridge: Bridge) -> AsyncBridge:
        """Wrap an already-connected Bridge for ``await`` use, sharing its link.

        Lets an async app reuse the one shared connection instead of opening a
        second:  ``esp = AsyncBridge.wrap(espbridge.connect(ble=False))``.
        ``close()`` is then a no-op for the wrapper — whoever owns the Bridge
        (e.g. the BridgeManager behind connect()) closes it.
        """
        self = cls.__new__(cls)
        self._kwargs = {}
        self._bridge = bridge
        self._owned = False
        return self

    @property
    def bridge(self) -> Bridge:
        """The underlying synchronous :class:`Bridge` (for events, ``on_event``,
        device drivers, or any method not surfaced here). Raises if not yet
        connected."""
        if self._bridge is None:
            raise RuntimeError(
                "AsyncBridge is not connected — use `async with AsyncBridge(...)` "
                "or `await bridge.connect()`"
            )
        return self._bridge

    async def connect(self) -> AsyncBridge:
        """Open the link off the event loop (Bridge() blocks while connecting).
        Usually you don't call this directly — use ``async with AsyncBridge():``."""
        if self._bridge is None:
            self._bridge = await asyncio.to_thread(Bridge, **self._kwargs)
        return self

    async def close(self) -> None:
        """Close the link off the event loop. No-op for a wrapper created with
        :meth:`wrap` (whoever owns the underlying Bridge closes it)."""
        if self._bridge is not None and self._owned:
            await asyncio.to_thread(self._bridge.close)
        self._bridge = None

    async def __aenter__(self) -> AsyncBridge:
        return await self.connect()

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def events(self, cmd: int, *, parse=None, maxsize: int = 0) -> _EventStream:
        """``async for`` over a raw firmware event ``cmd`` (payloads are bytes
        unless ``parse`` is given). See ``gpio_events()`` for the GPIO edge case."""
        return _EventStream(self.bridge, cmd, asyncio.get_running_loop(),
                            parse=parse, maxsize=maxsize)

    def gpio_events(self, *, maxsize: int = 0) -> _EventStream:
        """``async for ev in esp.gpio_events()`` yields :class:`EdgeEvent`s from
        every pin armed with ``await esp.gpio.watch(pin, ...)``."""
        return self.events(C.GPIO_EDGE_EVT, parse=EdgeEvent.parse, maxsize=maxsize)

    def watch_events(self, *, maxsize: int = 0) -> _EventStream:
        """``async for ev in esp.watch_events()`` yields :class:`WatchEvent`s
        from every rule defined with ``await esp.watch.add(...)`` — the polled,
        non-interrupt, threshold-based events."""
        return self.events(C.WATCH_EVT, parse=WatchEvent.parse, maxsize=maxsize)

    def __getattr__(self, name):
        if name.startswith("_"):  # never proxy private/dunder lookups
            raise AttributeError(name)
        attr = getattr(self.bridge, name)
        if name in Bridge._SUBAPIS:
            proxy = _AsyncSubApi(attr)
            setattr(self, name, proxy)  # cache: this name skips __getattr__ next time
            return proxy
        return _async_attr(attr)

    def __dir__(self):
        extra = (*Bridge._SUBAPIS, "bridge", "connect", "close",
                 "events", "gpio_events", "watch_events")
        return [*super().__dir__(), *extra]
