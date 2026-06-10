"""ESP-NOW: connectionless 2.4 GHz peer/broadcast messaging through the ESP32.

ESP-NOW coexists with Wi-Fi STA/AP and BLE (including the BLE link transport).
Channel rule: while the board is connected to a Wi-Fi network, ESP-NOW rides on
that network's channel and any requested channel is ignored — all peers must be
on the same channel to hear each other.

    b = espbridge.Bridge()
    mac = b.espnow.begin()                 # own MAC, give it to your peers
    b.espnow.add_peer("a0:b1:c2:d3:e4:f5")
    b.espnow.send("a0:b1:c2:d3:e4:f5", b"hello")   # True = peer ACKed
    b.espnow.on_receive(lambda mac, data, rssi: print(mac, data, rssi))
"""
from __future__ import annotations

import queue

from . import constants as C
from .errors import BridgeTimeoutError
from .protocol import mac_to_bytes, mac_to_str  # re-exported for back-compat

BROADCAST = "ff:ff:ff:ff:ff:ff"


class EspNow:
    """Connectionless ESP-NOW peer/broadcast messaging, reached as ``esp.espnow``.

        >>> esp = espbridge.connect()
        >>> esp.espnow.begin()                          # returns this board's MAC
        >>> esp.espnow.add_peer("a0:b1:c2:d3:e4:f5")
        >>> esp.espnow.send("a0:b1:c2:d3:e4:f5", b"hi")  # True when the peer ACKs
        >>> esp.espnow.broadcast(b"hello everyone")
        >>> esp.espnow.on_receive(lambda mac, data, rssi: print(mac, data, rssi))
    """

    def __init__(self, bridge):
        self._b = bridge
        self._rx_callbacks: list = []
        self._send_callbacks: list = []
        self._rx_queue: queue.Queue = queue.Queue()
        self._bcast_added = False
        bridge.on_event(C.ESPNOW_RX_EVT, self._on_rx)
        bridge.on_event(C.ESPNOW_SEND_EVT, self._on_send_result)

    # ---- events ---------------------------------------------------------------

    def _on_rx(self, p: bytes) -> None:
        if len(p) < 7:
            return
        mac = mac_to_str(p[:6])
        rssi = int.from_bytes(p[6:7], "big", signed=True)
        data = p[7:]
        if self._rx_callbacks:
            # Callback mode: deliver directly and skip the queue.
            # If we also queued, the queue would grow unbounded because
            # nothing would drain it.
            for cb in list(self._rx_callbacks):
                cb(mac, data, rssi)
        else:
            # Polled mode: buffer the packet so available()/read() can retrieve it.
            self._rx_queue.put_nowait((mac, data, rssi))

    def _on_send_result(self, p: bytes) -> None:
        if len(p) < 7:
            return
        mac, delivered = mac_to_str(p[:6]), p[6] == 0
        for cb in list(self._send_callbacks):
            cb(mac, delivered)

    def on_receive(self, callback) -> None:
        """callback(mac: str, data: bytes, rssi: int) for every incoming packet.

        With a callback registered, packets go to it instead of the
        available()/read() queue — use one style or the other.
        """
        self._rx_callbacks.append(callback)

    def on_send_result(self, callback) -> None:
        """callback(mac: str, delivered: bool) for send(..., wait=False) results."""
        self._send_callbacks.append(callback)

    # ---- polled alternative to on_receive ---------------------------------------

    def available(self) -> int:
        """Number of received packets waiting in the queue."""
        return self._rx_queue.qsize()

    def read(self, timeout: float | None = None) -> tuple[str, bytes, int]:
        """Pop the oldest received packet as (mac, data, rssi); blocks up to timeout."""
        try:
            return self._rx_queue.get(timeout=timeout)
        except queue.Empty:
            raise BridgeTimeoutError("no ESP-NOW packet received") from None

    # ---- commands ----------------------------------------------------------------

    def begin(self, channel: int = 0, *, long_range: bool = False) -> str:
        """Start ESP-NOW; returns this board's MAC address (give it to peers).

        channel 0 = stay on / inherit the current Wi-Fi channel (when the board
        is connected to Wi-Fi the channel is always inherited). long_range
        enables Espressif's proprietary LR PHY — more range, ESP32-to-ESP32 only.
        """
        self._b.require(C.Cap.ESPNOW, "ESP-NOW")
        if not 0 <= channel <= 14:
            raise ValueError(f"channel must be 0..14, got {channel}")
        mac = self._b.request(C.ESPNOW_INIT, bytes([channel, 1 if long_range else 0]))
        return mac_to_str(mac)

    def end(self) -> None:
        """Stop ESP-NOW (peers are forgotten; Wi-Fi stays as it was)."""
        self._b.request(C.ESPNOW_DEINIT)
        self._bcast_added = False

    def power_save(self, window_ms: int, interval_ms: int | None = None) -> None:
        """Trade receive reliability for radio power.

        RF listens for window_ms at the start of every interval_ms; packets
        arriving outside the window are lost. begin() defaults to 65535
        (always listening — right for powered boards); 0 turns RX off (TX
        still works). interval_ms None keeps the current interval."""
        if not 0 <= window_ms <= 65535:
            raise ValueError(f"window_ms must be 0..65535, got {window_ms}")
        if interval_ms is not None and not 1 <= interval_ms <= 65535:
            raise ValueError(f"interval_ms must be 1..65535, got {interval_ms}")
        payload = window_ms.to_bytes(2, "big") + (interval_ms or 0).to_bytes(2, "big")
        self._b.request(C.ESPNOW_POWER_SAVE, payload)

    def set_pmk(self, pmk: bytes) -> None:
        """Set the 16-byte Primary Master Key (do this before adding encrypted peers)."""
        if len(pmk) != 16:
            raise ValueError(f"PMK must be 16 bytes, got {len(pmk)}")
        self._b.request(C.ESPNOW_SET_PMK, pmk)

    def add_peer(self, mac: str | bytes, *, lmk: bytes | None = None,
                 channel: int = 0) -> None:
        """Register a peer. lmk = 16-byte Local Master Key to encrypt this link
        (both sides must use the same LMK, and set_pmk() first). channel 0 =
        follow the current Wi-Fi channel (recommended)."""
        if lmk is not None and len(lmk) != 16:
            raise ValueError(f"LMK must be 16 bytes, got {len(lmk)}")
        payload = mac_to_bytes(mac) + bytes([channel, 1 if lmk else 0]) + (lmk or b"")
        self._b.request(C.ESPNOW_ADD_PEER, payload)

    def remove_peer(self, mac: str | bytes) -> None:
        """Unregister a peer previously added with add_peer()."""
        self._b.request(C.ESPNOW_DEL_PEER, mac_to_bytes(mac))

    def send(self, mac: str | bytes, data: bytes, *, wait: bool = True) -> bool:
        """Send up to 250 bytes to a registered peer.

        wait=True (default) blocks a few ms and returns True when the peer's
        radio ACKed the packet (broadcasts are never ACKed -> always False).
        wait=False returns immediately; subscribe on_send_result() for the
        outcome. Use it for max-rate streaming.
        """
        if len(data) > C.ESPNOW_MAX_DATA:
            raise ValueError(f"ESP-NOW payload is limited to {C.ESPNOW_MAX_DATA} bytes")
        payload = mac_to_bytes(mac) + bytes(data)
        if not wait:
            self._b.send(C.ESPNOW_SEND, payload)
            return True
        return self._b.request(C.ESPNOW_SEND, payload)[0] == 1

    def broadcast(self, data: bytes, *, wait: bool = False) -> None:
        """Send up to 250 bytes to every listening board in range (never ACKed)."""
        if not self._bcast_added:  # auto-register the broadcast address on first use
            self.add_peer(BROADCAST)
            self._bcast_added = True
        self.send(BROADCAST, data, wait=wait)
