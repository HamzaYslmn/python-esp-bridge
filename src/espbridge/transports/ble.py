"""Bluetooth (BLE) transport — talk to the bridge with no USB cable.

The firmware advertises a Nordic-UART-style GATT service (see firmware
link_ble.cpp); this transport carries the exact same COBS frame stream over
it. Requires the `bleak` package: ``pip install python-esp-bridge[ble]``.

bleak is asyncio-based; a private event loop runs in a daemon thread so the
transport exposes the same blocking read()/write() interface as the serial
one. Notifications land in a queue that read() drains.
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass

from ..constants import BLE_LINK_RX_UUID, BLE_LINK_SERVICE_UUID, BLE_LINK_TX_UUID
from ..errors import BridgeError, NoDeviceError


ADV_NAME_PREFIX = "espbridge_"  # firmware advertises espbridge_<mac>[_<name>]


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
    import asyncio

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


class BleTransport:
    """BLE GATT transport with the same blocking interface as SerialTransport."""

    has_baud = False    # wireless: no baud negotiation
    needs_auth = True   # firmware requires SYS_AUTH before other commands
    usb_chip = None

    def __init__(self, address: str, *, connect_timeout: float = 10.0):
        _require_bleak()
        import asyncio

        self.address = address
        self._rx: queue.Queue[bytes] = queue.Queue()
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
        import asyncio

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
        # write-without-response payload limit for this connection
        self._chunk = max(20, client.mtu_size - 3)

    def _on_notify(self, _char, data: bytearray) -> None:
        self._rx.put(bytes(data))

    async def _write(self, data: bytes) -> None:
        for i in range(0, len(data), self._chunk):
            await self._client.write_gatt_char(
                BLE_LINK_RX_UUID, data[i : i + self._chunk], response=False)

    async def _disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass

    def read(self) -> bytes:
        try:
            return self._rx.get(timeout=0.05)
        except queue.Empty:
            return b""

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
        try:
            self._run(self._disconnect(), timeout=5.0)
        except Exception:
            pass
        self._stop_loop()
