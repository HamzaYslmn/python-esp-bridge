"""Ethernet — wired networking for the bridge (firmware opt-in).

Build the firmware with ``#define BRIDGE_ENABLE_ETH 1`` first, then:

    esp.eth.begin("wt32-eth01")          # board preset
    esp.eth.begin("w5500", cs=5, irq=4, rst=14)  # SPI module on any chip
    esp.eth.wait_for_ip()
    sock = esp.net.tcp_connect("example.com", 80)  # rides Ethernet now

Once the link has an IP, all esp.net sockets use it automatically.
"""
from __future__ import annotations

import struct
import threading

from . import constants as C
from .protocol import ip_str as _ip

# Stable wire phy ids (mirrors firmware mod_eth.cpp)
PHY = {"generic": 0, "lan8720": 1, "tlk110": 2, "ip101": 2, "rtl8201": 3,
       "dp83848": 4, "ksz8041": 5, "ksz8081": 6,
       "dm9051": 16, "w5500": 17, "ksz8851": 18}

CLK_GPIO0_IN, CLK_GPIO0_OUT, CLK_GPIO16_OUT, CLK_GPIO17_OUT = range(4)

# RMII board presets: phy, addr, mdc, mdio, power, clk  (classic ESP32 only)
PRESETS: dict[str, dict] = {
    "wt32-eth01":     dict(phy="lan8720", addr=1, mdc=23, mdio=18, power=16,
                           clk=CLK_GPIO0_IN),
    "olimex-poe":     dict(phy="lan8720", addr=0, mdc=23, mdio=18, power=12,
                           clk=CLK_GPIO17_OUT),
    "lilygo-t-poe":   dict(phy="lan8720", addr=0, mdc=23, mdio=18, power=5,
                           clk=CLK_GPIO17_OUT),
    "wesp32":         dict(phy="rtl8201", addr=0, mdc=16, mdio=17, power=-1,
                           clk=CLK_GPIO0_IN),
    "esp32-ethernet-kit": dict(phy="ip101", addr=1, mdc=23, mdio=18, power=5,
                               clk=CLK_GPIO0_IN),
}


class Eth:
    def __init__(self, bridge):
        self._b = bridge
        bridge.require(C.Cap.ETH, "Ethernet (BRIDGE_ENABLE_ETH=1 firmware)")
        self._got_ip = threading.Event()
        self._ip: str | None = None
        bridge.on_event(C.ETH_STATE_EVT, self._on_state)

    def _on_state(self, payload: bytes) -> None:
        if payload[0] == 2:  # got IP
            self._ip = _ip(payload[1:5])
            self._got_ip.set()
        elif payload[0] == 3:
            self._got_ip.clear()
            self._ip = None

    def begin(self, preset_or_phy: str, **kw) -> None:
        """Start Ethernet from a board preset name or a phy name + pins.

        RMII (classic ESP32): addr, mdc, mdio, power, clk.
        SPI (any chip, e.g. "w5500"): cs, irq, rst and optionally
        sck/miso/mosi (-1 = SPI defaults), freq_mhz.
        """
        cfg = dict(PRESETS.get(preset_or_phy, {"phy": preset_or_phy}), **kw)
        phy = PHY[cfg["phy"]]
        if phy >= 16:  # SPI PHY
            self._b.request(C.ETH_BEGIN_SPI, struct.pack(
                ">BbbbbbbbB", phy, cfg.get("addr", 1),
                cfg["cs"], cfg.get("irq", -1), cfg.get("rst", -1),
                cfg.get("sck", -1), cfg.get("miso", -1), cfg.get("mosi", -1),
                cfg.get("freq_mhz", 20)), timeout=10.0)
        else:
            self._b.request(C.ETH_BEGIN_RMII, struct.pack(
                ">BbbbbB", phy, cfg.get("addr", 0), cfg.get("mdc", 23),
                cfg.get("mdio", 18), cfg.get("power", -1),
                cfg.get("clk", CLK_GPIO0_IN)), timeout=10.0)

    def wait_for_ip(self, timeout: float = 15.0) -> str:
        """Block until DHCP hands out an address; returns it."""
        if not self._got_ip.wait(timeout):
            raise TimeoutError("no IP from DHCP (cable? link lights?)")
        return self._ip or self.status()["ip"]

    def status(self) -> dict:
        r = self._b.request(C.ETH_STATUS)
        return {"link": bool(r[0]), "ip": _ip(r[1:5]), "gateway": _ip(r[5:9]),
                "netmask": _ip(r[9:13]),
                "mac": ":".join(f"{b:02x}" for b in r[13:19])}

    def end(self) -> None:
        self._b.request(C.ETH_STOP)
