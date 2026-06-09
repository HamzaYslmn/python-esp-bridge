"""AsyncBridge: await sub-API calls, fan out with gather, stream edge events."""
import asyncio
import struct

import pytest

from espbridge import AsyncBridge
from espbridge import constants as C


def _async(fw, scenario):
    """Run an async scenario(esp) against the fake firmware."""
    async def runner():
        async with AsyncBridge(transport=fw.transport, upgrade_baud=False,
                               reset_on_open=False) as esp:
            await scenario(esp)
    asyncio.run(runner())


def test_await_subapi_calls(fw):
    async def scenario(esp):
        await esp.gpio.mode(2, "output")
        assert await esp.gpio.write(2, 1) == 1   # write returns the read-back level
        assert await esp.gpio.read(2) == 1
    _async(fw, scenario)


def test_plain_attributes_pass_through(fw):
    async def scenario(esp):
        # info/caps are local — exposed directly, not as coroutines.
        assert esp.info.chip.name
        assert not asyncio.iscoroutine(esp.info)
        assert C.Cap.WIFI in esp.caps
    _async(fw, scenario)


def test_bridge_method_awaitable(fw):
    async def scenario(esp):
        assert isinstance(await esp.ping(), float)
        assert (await esp.free_heap())["free"] > 0
    _async(fw, scenario)


def test_gather_runs_concurrently(fw):
    async def scenario(esp):
        await esp.gpio.mode(2, "output")
        await esp.gpio.write(2, 1)
        # Many reads issued together; each runs in its own thread and the
        # results come back correctly correlated by sequence number.
        results = await asyncio.gather(*(esp.gpio.read(2) for _ in range(8)))
        assert results == [1] * 8
    _async(fw, scenario)


def test_gpio_event_stream(fw):
    async def scenario(esp):
        await esp.gpio.mode(4, "input_pullup")
        await esp.gpio.watch(4, edge="falling", debounce_ms=30)
        stream = esp.gpio_events()
        fw.emit(C.GPIO_EDGE_EVT, bytes([4, 0]) + struct.pack(">I", 12345))
        ev = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        assert ev.pin == 4 and ev.level == 0 and ev.millis == 12345
        await stream.aclose()
    _async(fw, scenario)


def test_not_connected_raises():
    esp = AsyncBridge(ble=False)
    with pytest.raises(RuntimeError, match="not connected"):
        esp.gpio  # noqa: B018 — sub-API access before connect() must explain itself
