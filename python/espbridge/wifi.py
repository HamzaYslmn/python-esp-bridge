"""Wi-Fi management: scan, STA connect, AP mode, status."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from . import constants as C
from .errors import BridgeTimeoutError
from .protocol import ip_str as _ip
from .protocol import lp, mac_to_str

WL_CONNECTED = 3  # wl_status_t value meaning "connected" (matches Arduino WiFi.h)

AUTH_MODES = {0: "open", 1: "wep", 2: "wpa_psk", 3: "wpa2_psk", 4: "wpa_wpa2_psk",
              5: "wpa2_enterprise", 6: "wpa3_psk", 7: "wpa2_wpa3_psk", 8: "wapi_psk"}


@dataclass(frozen=True)
class Network:
    """One access point found by scan() (SSID, signal, BSSID, channel, auth mode)."""

    ssid: str
    rssi: int
    bssid: str
    channel: int
    auth: str


@dataclass(frozen=True)
class WifiStatus:
    """Snapshot of the STA interface (status code, IP config, RSSI, channel, MAC)."""

    status: int
    ip: str
    gateway: str
    netmask: str
    rssi: int
    channel: int
    mac: str

    @property
    def connected(self) -> bool:
        """True when the STA is associated with an access point."""
        return self.status == WL_CONNECTED


LINK_STATES = {0: "off", 1: "joining", 2: "listening", 3: "connected"}


@dataclass(frozen=True)
class LinkStatus:
    """State of the Wi-Fi *transport* link — how the host reaches this board.
    ``state`` is off / joining / listening (which covers dialing out to a host)
    / connected; ``ip`` and ``port`` say where a listening board answers."""

    state: str
    ip: str
    port: int
    peer: bool
    tx_errors: int


class Wifi:
    """Wi-Fi station/AP control: scan, join, status, host an access point.

    Reached as ``esp.wifi`` where ``esp = espbridge.connect()``.

        >>> esp = espbridge.connect()
        >>> for net in esp.wifi.scan():
        ...     print(net.ssid, net.rssi, net.auth)
        >>> st = esp.wifi.connect("ssid", "password")
        >>> st.ip
        '192.168.1.42'
        >>> esp.wifi.status().connected
        True
        >>> esp.wifi.disconnect()
    """

    def __init__(self, bridge):
        self._b = bridge
        self._scan_results: list[Network] = []
        self._scan_done = threading.Event()
        self._state_callbacks: list = []
        bridge.on_event(C.WIFI_SCAN_RES, self._on_scan_res)
        bridge.on_event(C.WIFI_SCAN_DONE, self._on_scan_done)
        bridge.on_event(C.WIFI_STATE_EVT, self._on_state)

    # ---- event handlers — all called on the reader thread --------------------

    def _on_scan_res(self, p: bytes) -> None:
        if len(p) < 12:
            return
        ssid = p[12 : 12 + p[11]].decode("utf-8", "replace")
        rssi = int.from_bytes(p[2:3], "big", signed=True)
        bssid = mac_to_str(p[5:11])
        self._scan_results.append(
            Network(ssid, rssi, bssid, p[4], AUTH_MODES.get(p[3], str(p[3])))
        )

    def _on_scan_done(self, p: bytes) -> None:
        self._scan_done.set()

    def _on_state(self, p: bytes) -> None:
        if not p:
            return
        event = {1: "connected", 2: "got_ip", 3: "disconnected"}.get(p[0], str(p[0]))
        ip = _ip(p[1:5]) if len(p) >= 5 else "0.0.0.0"
        for cb in list(self._state_callbacks):
            cb(event, ip)

    def on_state(self, callback) -> None:
        """callback(event: str, ip: str) on connect/got_ip/disconnect."""
        self._state_callbacks.append(callback)

    # ---- commands — all block until the firmware responds --------------------

    def scan(self, timeout: float = 15.0) -> list[Network]:
        """Scan for access points; returns Networks sorted by signal (strongest first)."""
        self._scan_results = []
        self._scan_done.clear()
        self._b.request(C.WIFI_SCAN)
        if not self._scan_done.wait(timeout):
            raise BridgeTimeoutError("Wi-Fi scan did not finish")
        return sorted(self._scan_results, key=lambda n: -n.rssi)

    def connect(self, ssid: str, password: str = "", *, wait: bool = True,
                timeout: float = 20.0) -> WifiStatus:
        """Join an access point. wait=True blocks until an IP is obtained or `timeout`."""
        self._b.request(C.WIFI_CONNECT, lp(ssid) + lp(password))
        if not wait:
            return self.status()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.status()
            if st.connected and st.ip != "0.0.0.0":
                return st
            time.sleep(0.25)
        raise BridgeTimeoutError(f"could not join {ssid!r} within {timeout}s")

    def disconnect(self) -> None:
        """Disconnect from the current access point."""
        self._b.request(C.WIFI_DISCONNECT)

    def status(self) -> WifiStatus:
        """Return the current STA status (connection state, IP config, RSSI, MAC)."""
        p = self._b.request(C.WIFI_STATUS)
        return WifiStatus(
            status=p[0],
            ip=_ip(p[1:5]),
            gateway=_ip(p[5:9]),
            netmask=_ip(p[9:13]),
            rssi=int.from_bytes(p[13:14], "big", signed=True),
            channel=p[14],
            mac=mac_to_str(p[15:21]),
        )

    def ap_start(self, ssid: str, password: str = "", *, channel: int = 1,
                 max_conn: int = 4) -> str:
        """Start an access point; returns its IP (clients usually get 192.168.4.x)."""
        payload = lp(ssid) + lp(password) + bytes([channel, max_conn])
        return _ip(self._b.request(C.WIFI_AP_START, payload, timeout=5.0))

    def ap_stop(self) -> None:
        """Stop the access point started by ap_start()."""
        self._b.request(C.WIFI_AP_STOP)

    def hostname(self, name: str) -> None:
        """Set the DHCP hostname used on the network (call before connect())."""
        self._b.request(C.WIFI_HOSTNAME, lp(name))

    # ---- the Wi-Fi transport link (bridge protocol over TCP) ------------------
    # Not the same thing as connect() above: that joins a network so NET sockets
    # can be proxied; these provision the link the HOST talks to the board over.

    def link_setup(self, ssid: str, password: str = "", *, server: str = "",
                   port: int = C.BRIDGE_LINK_PORT, persist: bool = True,
                   start: bool = True) -> None:
        """Provision the Wi-Fi transport link (send this over USB or Bluetooth).

        ``server=""`` is listen mode: the board waits on ``port`` for
        ``Bridge(host=...)``. A ``"host"`` or ``"host:port"`` makes it dial your
        host and reconnect forever, where ``Bridge(wifi=True)`` picks it up —
        the mode to use past a handful of boards.

        Credentials persist in NVS, so the board comes back on Wi-Fi after every
        reboot with no reflash. Same password as Bluetooth.

            >>> esp.wifi.link_setup("ssid", "pass")                      # listen
            >>> esp.wifi.link_setup("ssid", "pass", server="10.0.0.5")  # dials home
        """
        flags = (0x01 if persist else 0) | (0x02 if start else 0)
        payload = (lp(ssid) + lp(password) + lp(server)
                   + port.to_bytes(2, "big") + bytes([flags]))
        self._b.request(C.WIFI_LINK_SETUP, payload, timeout=10.0)

    def link_stop(self) -> None:
        """Stop the Wi-Fi transport link and erase its stored configuration."""
        self._b.request(C.WIFI_LINK_STOP, timeout=5.0)

    def link_status(self) -> LinkStatus:
        """State of the Wi-Fi transport link (off / joining / listening / connected)."""
        p = self._b.request(C.WIFI_LINK_STATUS)
        return LinkStatus(
            state=LINK_STATES.get(p[0], "unknown"),
            ip=_ip(p[1:5]),
            port=int.from_bytes(p[5:7], "big"),
            peer=bool(p[7]),
            tx_errors=int.from_bytes(p[8:12], "big"),
        )
