"""BLE: scanning, advertising, GATT server and basic GATT client."""
from __future__ import annotations

import queue
import struct
import threading
import time
import uuid as _uuid
from dataclasses import dataclass

from . import constants as C
from .errors import BridgeTimeoutError
from .protocol import lp, mac_to_str


def to_uuid128(value: int | str | _uuid.UUID) -> bytes:
    """16/32-bit ints expand into the Bluetooth base UUID; strs/UUIDs pass through."""
    if isinstance(value, int):
        return _uuid.UUID(f"{value:08x}-0000-1000-8000-00805f9b34fb").bytes
    if isinstance(value, str):
        return _uuid.UUID(value).bytes
    return value.bytes


@dataclass(frozen=True)
class Advertisement:
    """A single BLE advertisement seen during a scan (address, RSSI, raw AD payload)."""

    addr: str
    addr_type: int
    rssi: int
    data: bytes  # raw advertisement payload (AD structures)

    @property
    def name(self) -> str | None:
        """Local name parsed from the AD structures, if present."""
        i, d = 0, self.data
        while i + 1 < len(d):
            length = d[i]
            if length == 0 or i + 1 + length > len(d) + 1:
                break
            ad_type = d[i + 1]
            if ad_type in (0x08, 0x09):  # AD type 0x08 = Shortened Local Name, 0x09 = Complete Local Name
                return d[i + 2 : i + 1 + length].decode("utf-8", "replace")
            i += 1 + length
        return None


class Characteristic:
    """A characteristic of the local GATT server."""

    def __init__(self, ble: "Ble", char_id: int, uuid_: bytes):
        self._ble = ble
        self.char_id = char_id
        self.uuid = uuid_
        self.on_write = None  # callback(bytes)

    def set(self, data: bytes) -> None:
        """Update the stored value clients will get on the next read."""
        self._ble._b.request(C.BLE_GATTS_SET, bytes([self.char_id]) + bytes(data))

    def notify(self, data: bytes) -> None:
        """Push `data` to subscribed clients as a notification (requires Ble.NOTIFY)."""
        self._ble._b.request(C.BLE_GATTS_NTFY, bytes([self.char_id]) + bytes(data))


class GattClient:
    """Connection to a remote BLE peripheral (one at a time)."""

    def __init__(self, ble: "Ble"):
        self._ble = ble
        self.connected = True

    def read(self, service, char) -> bytes:
        """Read a remote characteristic's value (UUIDs as int/str/UUID)."""
        return self._ble._b.request(
            C.BLE_GATTC_READ, to_uuid128(service) + to_uuid128(char), timeout=10.0)

    def write(self, service, char, data: bytes) -> None:
        """Write `data` to a remote characteristic (UUIDs as int/str/UUID)."""
        self._ble._b.request(
            C.BLE_GATTC_WRITE, to_uuid128(service) + to_uuid128(char) + bytes(data),
            timeout=10.0)

    def subscribe(self, service, char, callback) -> None:
        """callback(bytes) for notifications from this characteristic."""
        self._ble._notify_callbacks[to_uuid128(char)] = callback
        self._ble._b.request(
            C.BLE_GATTC_SUB, to_uuid128(service) + to_uuid128(char) + b"\x01",
            timeout=10.0)

    def unsubscribe(self, service, char) -> None:
        """Stop notifications from a characteristic previously passed to subscribe()."""
        self._ble._notify_callbacks.pop(to_uuid128(char), None)
        self._ble._b.request(
            C.BLE_GATTC_SUB, to_uuid128(service) + to_uuid128(char) + b"\x00",
            timeout=10.0)

    def disconnect(self) -> None:
        """Drop the link to the peripheral."""
        self.connected = False
        self._ble._b.request(C.BLE_GATTC_DISC, timeout=10.0)


class Ble:
    """BLE scanning, advertising, GATT server and a single-link GATT client.

    Reached as ``esp.ble`` where ``esp = espbridge.connect()``.

        >>> esp = espbridge.connect()
        >>> for adv in esp.ble.scan(5.0):      # central: discover nearby devices
        ...     print(adv.addr, adv.rssi, adv.name)
        >>> cli = esp.ble.connect("aa:bb:cc:dd:ee:ff")
        >>> cli.read(0x180f, 0x2a19)           # battery service / level
        >>> cli.disconnect()
        >>>
        >>> # peripheral: serve a characteristic and advertise
        >>> ch, = esp.ble.serve(0x180f, [(0x2a19, Ble.READ | Ble.NOTIFY)])
        >>> esp.ble.advertise("my-esp")
        >>> ch.set(b"\\x64")
    """

    # characteristic property flags
    READ = C.GATT_PROP_READ
    WRITE = C.GATT_PROP_WRITE
    NOTIFY = C.GATT_PROP_NOTIFY
    WRITE_NR = C.GATT_PROP_WRITE_NR

    def __init__(self, bridge):
        self._b = bridge
        bridge.require(C.Cap.BLE_FW, "BLE (firmware built without it?)")
        self._adv_queue: queue.Queue[Advertisement] = queue.Queue(maxsize=512)
        self._adv_callbacks: list = []
        self._chars: dict[int, Characteristic] = {}
        self._notify_callbacks: dict[bytes, object] = {}
        self._server_connected = threading.Event()
        bridge.on_event(C.BLE_ADV_EVT, self._on_adv)
        bridge.on_event(C.BLE_GATTS_WR_EVT, self._on_gatts_write)
        bridge.on_event(C.BLE_GATTS_CONN_EVT, self._on_gatts_conn)
        bridge.on_event(C.BLE_GATTC_NTFY_EVT, self._on_notify)

    # ---- event handlers — all called on the reader thread -----------------------

    def _on_adv(self, p: bytes) -> None:
        if len(p) < 8:
            return
        adv = Advertisement(
            addr=mac_to_str(p[0:6]),
            addr_type=p[6],
            rssi=int.from_bytes(p[7:8], "big", signed=True),
            data=p[8:],
        )
        for cb in list(self._adv_callbacks):
            cb(adv)
        try:
            self._adv_queue.put_nowait(adv)
        except queue.Full:
            pass

    def _on_gatts_write(self, p: bytes) -> None:
        if not p:
            return
        ch = self._chars.get(p[0])
        if ch is not None and ch.on_write is not None:
            ch.on_write(p[1:])

    def _on_gatts_conn(self, p: bytes) -> None:
        if p and p[0]:
            self._server_connected.set()
        else:
            self._server_connected.clear()

    def _on_notify(self, p: bytes) -> None:
        if len(p) < 16:
            return
        cb = self._notify_callbacks.get(bytes(p[:16]))
        if cb is not None:
            cb(p[16:])

    # ---- scanning --------------------------------------------------------------------

    def scan(self, duration: float = 5.0, *, active: bool = True,
             callback=None) -> list[Advertisement]:
        """Collect advertisements for `duration` seconds.

        With `callback`, advertisements also stream to it live. Returns devices
        deduplicated by address (strongest RSSI kept).
        """
        if callback is not None:
            self._adv_callbacks.append(callback)
        while not self._adv_queue.empty():  # discard any packets buffered before this scan
            self._adv_queue.get_nowait()
        self._b.request(C.BLE_SCAN_START, bytes([min(255, int(duration) + 1), 1 if active else 0]),
                        timeout=10.0)
        seen: dict[str, Advertisement] = {}
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            try:
                adv = self._adv_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            cur = seen.get(adv.addr)
            if cur is None or adv.rssi > cur.rssi or (adv.name and not cur.name):
                seen[adv.addr] = adv
        self._b.request(C.BLE_SCAN_STOP, timeout=10.0)
        if callback is not None:
            self._adv_callbacks.remove(callback)
        return sorted(seen.values(), key=lambda a: -a.rssi)

    # ---- advertising ----------------------------------------------------------------

    def advertise(self, name: str = "", *, manufacturer_data: bytes = b"",
                  service_uuid16: int = 0) -> None:
        """Start advertising so centrals can discover and connect to this board."""
        payload = lp(name) + lp(manufacturer_data) + struct.pack(">H", service_uuid16)
        self._b.request(C.BLE_ADV_START, payload, timeout=10.0)

    def advertise_stop(self) -> None:
        """Stop advertising."""
        self._b.request(C.BLE_ADV_STOP, timeout=10.0)

    # ---- GATT server -----------------------------------------------------------------

    def serve(self, service, chars: list[tuple[object, int]]) -> list[Characteristic]:
        """Define a GATT service. `chars` is [(uuid, props), ...] with props a
        bitmask of Ble.READ / Ble.WRITE / Ble.NOTIFY / Ble.WRITE_NR.
        Call advertise() afterwards to become discoverable."""
        payload = to_uuid128(service) + bytes([len(chars)])
        for u, props in chars:
            payload += to_uuid128(u) + bytes([props])
        ids = self._b.request(C.BLE_GATTS_DEF, payload, timeout=10.0)
        out = []
        for (u, _), cid in zip(chars, ids):
            ch = Characteristic(self, cid, to_uuid128(u))
            self._chars[cid] = ch
            out.append(ch)
        return out

    def wait_connect(self, timeout: float | None = None) -> bool:
        """Block until a client connects to the GATT server; True if one did."""
        return self._server_connected.wait(timeout)

    # ---- GATT client ------------------------------------------------------------------

    def connect(self, addr: str, addr_type: int = 0, timeout: float = 15.0) -> GattClient:
        """Connect to a peripheral by address ("aa:bb:cc:dd:ee:ff")."""
        mac = bytes(int(x, 16) for x in addr.split(":"))
        if len(mac) != 6:
            raise ValueError("address must be 6 bytes, e.g. 'aa:bb:cc:dd:ee:ff'")
        self._b.request(C.BLE_GATTC_CONN, mac + bytes([addr_type]), timeout=timeout)
        return GattClient(self)
