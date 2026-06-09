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
import queue
import threading
from dataclasses import dataclass

from .._log import log
from ..constants import BLE_LINK_RX_UUID, BLE_LINK_SERVICE_UUID, BLE_LINK_TX_UUID
from ..errors import BridgeError, NoDeviceError


ADV_NAME_PREFIX = "espbridge_"  # firmware advertises espbridge_<mac>[_<name>]

_RX_SHUTDOWN = object()  # sentinel put on the RX queue to wake a blocked read()


@dataclass(frozen=True)
class BleDeviceInfo:
    name: str     # advertised name: "espbridge_c049efd03fe0" or "espbridge_c049efd03fe0_relays"
    address: str  # MAC on Windows/Linux, CoreBluetooth UUID on macOS
    rssi: int

    def _parts(self) -> tuple[str, str]:
        rest = self.name.removeprefix(ADV_NAME_PREFIX)
        hexpart, _, custom = rest.partition("_")
        if len(hexpart) == 12 and all(c in "0123456789abcdef" for c in hexpart):
            return hexpart, custom
        return "", ""

    @property
    def mac(self) -> str:
        """Bridge MAC (matches Info.mac) parsed from the advertised name."""
        hexpart, _ = self._parts()
        return ":".join(hexpart[i : i + 2] for i in range(0, 12, 2)) if hexpart else ""

    @property
    def device_name(self) -> str:
        """User-assigned name (espbridge set-name), "" when unset."""
        return self._parts()[1]


def _require_bleak():
    try:
        import bleak  # noqa: F401
    except ImportError:
        raise BridgeError(
            "the Bluetooth transport needs the 'bleak' package — "
            "install with: pip install python-esp-bridge[ble]"
        ) from None


def find_ble_devices(timeout: float = 5.0) -> list[BleDeviceInfo]:
    """Scan for bridges advertising the BLE link service."""
    _require_bleak()
    from bleak import BleakScanner

    async def _scan():
        found = await BleakScanner.discover(
            timeout=timeout, return_adv=True,
            service_uuids=[BLE_LINK_SERVICE_UUID],
        )
        return [
            BleDeviceInfo(dev.name or "", dev.address, adv.rssi)
            for dev, adv in found.values()
        ]

    return asyncio.run(_scan())


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

    async def _scan():
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

    return asyncio.run(_scan())


class BleTransport:
    """BLE GATT transport with the same blocking interface as SerialTransport."""

    has_baud = False    # wireless: no baud negotiation
    needs_auth = True   # firmware requires SYS_AUTH before other commands
    usb_chip = None
    # Max unacknowledged fire-and-forget bytes before Bridge.send() inserts a
    # ping fence. The firmware buffers BLE writes in a LINK_RX_BUF (6144 B)
    # stream buffer that drains only as fast as commands execute; BLE delivers
    # faster than a 1 KB I2C write executes (~23 ms at 400 kHz), so an
    # unthrottled burst overflows it and frames are dropped (-> host timeout).
    burst_window = 4096
    # Same cap, applied to *waited* requests too: pipelined/concurrent requests
    # (several threads, or large echoes) would otherwise flood the same buffer
    # because the slow BLE notify reply-path can't complete them fast enough.
    # Bounds in-flight bytes to ~one frame past this, well under LINK_RX_BUF.
    # Serial leaves this unset: it drains at line rate and self-paces, so
    # capping it would needlessly throttle its pipelining.
    max_inflight = 4096

    def __init__(self, address: str, *, connect_timeout: float = 10.0):
        _require_bleak()
        self.address = address
        self._rx: queue.Queue = queue.Queue()
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
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

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
            try:
                await self._client.disconnect()
            except Exception:
                pass

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
        self._stop_loop()
