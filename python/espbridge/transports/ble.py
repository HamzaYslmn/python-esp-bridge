"""Bluetooth (BLE) transport — talk to the bridge with no USB cable.

The firmware advertises a Nordic-UART-style GATT service (see firmware
link_ble.cpp); this transport carries the exact same COBS frame stream over
it. Requires the `bleak` package: ``pip install python-esp-bridge[ble]``.

bleak is asyncio-based; a private event loop runs in a daemon thread so the
transport exposes the same blocking read()/write() interface as the serial
one. Notifications land in a queue that read() drains.
"""
from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
from dataclasses import dataclass

from .._log import log
from ..constants import BLE_LINK_RX_UUID, BLE_LINK_SERVICE_UUID, BLE_LINK_TX_UUID
from ..errors import BridgeError, NoDeviceError


# Bridges advertise as "espbridge_<identity>", where the identity is the stored
# name or, until one is set, the 12-hex MAC. The prefix marks the board as ours
# in any BLE scanner; discovery additionally filters on BLE_LINK_SERVICE_UUID.
ADV_NAME_PREFIX = "espbridge_"

_RX_SHUTDOWN = object()  # sentinel put on the RX queue to wake a blocked read()


@dataclass(frozen=True)
class BleDeviceInfo:
    advertised: str  # the raw advertised name, "espbridge_<identity>"
    address: str     # MAC on Windows/Linux, CoreBluetooth UUID on macOS
    rssi: int

    @property
    def _advertised_identity(self) -> str:
        """Whatever follows the prefix; "" if this device isn't one of ours."""
        rest = self.advertised.removeprefix(ADV_NAME_PREFIX)
        return "" if rest == self.advertised else rest

    @property
    def mac(self) -> str:
        """The board's MAC, when the advertisement carries it — i.e. while the
        board is unnamed, since only one identity fits. "" otherwise, and
        ``Bridge(mac=...)`` then falls back to asking each board over the link.

        It is *not* ``address``, which is the OS's handle for the radio — a UUID
        on macOS, and elsewhere the Bluetooth MAC, which differs from the base
        MAC the firmware reports.
        """
        hexpart = self._advertised_identity
        if len(hexpart) != 12 or any(c not in "0123456789abcdef" for c in hexpart):
            return ""
        return ":".join(hexpart[i : i + 2] for i in range(0, 12, 2))

    @property
    def name(self) -> str:
        """Stored device name (see Bridge.set_name), "" while unnamed.

        Whole, never truncated: BRIDGE_NAME_MAX is chosen so ``espbridge_`` plus
        the name fits the advertisement, so this is the string SYS_INFO reports.
        """
        return "" if self.mac else self._advertised_identity

    @property
    def ident(self) -> str:
        """What to pass to ``Bridge()`` for this board — mirrors ``Info.ident``."""
        return self.name or self.mac


def _require_bleak():
    try:
        import bleak  # noqa: F401 — importing IS the availability probe
    except ImportError:
        raise BridgeError(
            "the Bluetooth transport needs the 'bleak' package — "
            "install with: pip install python-esp-bridge[ble]"
        ) from None


def _scan(coro_fn):
    """Run a bleak scan, turning an unusable adapter into a NoDeviceError.

    bleak raises when Bluetooth is off, missing or blocked by OS permissions,
    and a traceback out of the scanner tells a user nothing they can act on.
    NoDeviceError is also what lets the default transport order fall through to
    USB serial on a machine with no Bluetooth at all (see bridge._candidates).
    """
    from bleak.exc import BleakError   # callers checked bleak is installed

    try:
        return asyncio.run(coro_fn())
    except BleakError as e:
        raise NoDeviceError(f"Bluetooth is unavailable: {e}") from None


def find_ble_devices(timeout: float = 5.0) -> list[BleDeviceInfo]:
    """Scan for bridges advertising the BLE link service."""
    _require_bleak()
    from bleak import BleakScanner

    async def _run():
        found = await BleakScanner.discover(
            timeout=timeout, return_adv=True,
            service_uuids=[BLE_LINK_SERVICE_UUID],
        )
        return [
            BleDeviceInfo(dev.name or "", dev.address, adv.rssi)
            for dev, adv in found.values()
        ]

    return _scan(_run)


def find_ble_devices_fast(match=None, *, settle: float = 0.4,
                          timeout: float = 10.0) -> list[BleDeviceInfo]:
    """Scan for bridges, returning as soon as one shows up.

    Unlike find_ble_devices (which always waits the full timeout to enumerate
    every board), this stops `settle` seconds after the first *matching* bridge
    is seen — so a connect pays roughly the time-to-first-advertisement instead
    of the whole scan window. `match` is an optional predicate on a
    BleDeviceInfo. Returns the matches found (strongest RSSI first), or [] if
    none appeared within `timeout`.
    """
    _require_bleak()
    from bleak import BleakScanner

    async def _run():
        seen: dict[str, BleDeviceInfo] = {}

        def _cb(dev, adv):
            info = BleDeviceInfo(dev.name or "", dev.address, adv.rssi)
            if match is not None and not match(info):
                return
            seen[dev.address] = info

        scanner = BleakScanner(detection_callback=_cb,
                               service_uuids=[BLE_LINK_SERVICE_UUID])
        await scanner.start()
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline and not seen:
                await asyncio.sleep(0.05)
            if seen:
                await asyncio.sleep(settle)  # brief window to catch siblings
        finally:
            await scanner.stop()
        return sorted(seen.values(), key=lambda d: d.rssi, reverse=True)

    return _scan(_run)


class BleTransport:
    """BLE GATT transport with the same blocking interface as SerialTransport."""

    has_baud = False    # wireless: no baud negotiation
    needs_auth = True   # firmware requires SYS_AUTH before other commands
    usb_chip = None
    # Max unacknowledged fire-and-forget bytes before Bridge.send() inserts a
    # ping fence. The firmware buffers BLE writes in a LINK_RX_BUF (6400 B)
    # stream buffer that drains only as fast as commands execute; BLE delivers
    # faster than a 1 KB I2C write executes (~23 ms at 400 kHz), so an
    # unthrottled burst overflows it and frames are dropped (-> host timeout).
    # 4300 = LINK_RX_BUF minus one max-size frame; two frames pipeline, which
    # already saturates BLE (the central's write rate is the bottleneck).
    burst_window = 4300
    # Same cap on *waited* requests: pipelined/concurrent requests (several
    # threads, or large echoes) would otherwise flood the same buffer because
    # the slow BLE notify reply-path can't drain it fast enough.
    max_inflight = 4300

    def __init__(self, address: str, *, connect_timeout: float = 10.0):
        _require_bleak()
        self.address = address
        self._rx: queue.Queue = queue.Queue()
        self._closed = False
        self._chunk_size = 20  # write-without-response size; grows after MTU exchange
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        daemon=True, name="espbridge-ble")
        self._thread.start()
        self._client = None
        try:
            self._run(self._connect(address, connect_timeout), connect_timeout + 5)
        except BaseException:
            self._stop_loop()
            raise

    def _run(self, coro, timeout: float):
        from bleak.exc import BleakError

        if self._closed:
            # The loop is gone, so this coroutine would never be awaited — and
            # Python reports that as a RuntimeWarning from deep inside asyncio.
            # Writes after close() are real (a driver's __del__ flushing, say);
            # say so plainly instead.
            coro.close()
            raise BridgeError("the Bluetooth link is closed")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except BleakError as e:
            # A dropout mid-session surfaces as bleak's "Not connected", which
            # no caller of this library should have to know about: everything
            # that goes wrong on a link is a BridgeError.
            raise BridgeError(f"Bluetooth link lost: {e}") from None

    async def _connect(self, address: str, timeout: float) -> None:
        from bleak import BleakClient
        from bleak.exc import BleakError

        client = BleakClient(address, timeout=timeout)
        try:
            await client.connect()
            await client.start_notify(BLE_LINK_TX_UUID, self._on_notify)
        except BleakError as e:
            raise NoDeviceError(f"BLE connect to {address} failed: {e}") from None
        self._client = client
        self._rx_char = client.services.get_characteristic(BLE_LINK_RX_UUID)
        # The write-without-response limit starts at 20 and jumps after the
        # MTU exchange (bleak docs); wait briefly so frames are not shredded
        # into 20-byte writes during the handshake.
        for _ in range(20):
            if self._rx_char.max_write_without_response_size > 20:
                log.debug(f"BLE write chunk size: "
                          f"{self._rx_char.max_write_without_response_size}")
                break
            await asyncio.sleep(0.1)
        else:
            log.warning("BLE MTU exchange did not complete; writes will be "
                        "chunked at 20 bytes (slow link)")
        # Cache the negotiated chunk size: stable once the MTU exchange lands,
        # so per-write attribute reads are unnecessary.
        self._chunk_size = max(20, self._rx_char.max_write_without_response_size)

    def _on_notify(self, _char, data: bytearray) -> None:
        self._rx.put(bytes(data))

    async def _write(self, data: bytes) -> None:
        chunk = self._chunk_size  # cached at connect; no per-write attribute read
        mv = memoryview(data)     # zero-copy chunk slices
        for i in range(0, len(data), chunk):
            await self._client.write_gatt_char(
                self._rx_char, mv[i : i + chunk], response=False)

    async def _disconnect(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()

    def read(self) -> bytes:
        # Block until a notification lands (or close() injects the sentinel) —
        # no idle polling, and inbound frames hand off the instant they arrive.
        item = self._rx.get()
        return b"" if item is _RX_SHUTDOWN else item

    def write(self, data: bytes) -> None:
        self._run(self._write(data), timeout=10.0)

    def set_baudrate(self, baud: int) -> None:
        pass  # wireless

    def pulse_reset(self) -> None:
        pass  # no DTR/RTS lines over the air

    def _stop_loop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)

    def close(self) -> None:
        self._rx.put(_RX_SHUTDOWN)  # wake the reader thread blocked in read()
        try:
            self._run(self._disconnect(), timeout=5.0)
        except Exception:
            pass
        self._closed = True         # after the disconnect, which goes via _run
        self._stop_loop()
