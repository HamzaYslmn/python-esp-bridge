"""Wi-Fi link: the TCP transport, dialled out and dialled home.

Both run the real socket code against the in-process FakeFirmware — the same
frame stream the USB and BLE links carry, so nothing here is a protocol mock.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from espbridge import Bridge
from espbridge.errors import AuthError, BridgeTimeoutError
from fake_firmware import FakeFirmware

PASSWORD = "espbridge"


def _pump(fw: FakeFirmware, conn: socket.socket) -> None:
    """Run one FakeFirmware over a connected socket until the peer goes away."""
    fw.transport.inject = lambda data: conn.sendall(data)  # replies go to the wire
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                break
            fw._on_host_bytes(data)
    except OSError:
        pass
    finally:
        conn.close()


class FakeBoardServer:
    """A board in *listen* mode: accepts one host connection (Bridge(host=...))."""

    def __init__(self, fw: FakeFirmware):
        self.fw = fw
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        _pump(self.fw, conn)

    def close(self) -> None:
        self.sock.close()


def dial_home(fw: FakeFirmware, port: int) -> None:
    """A board in *dial-home* mode: connects out to Bridge(wifi=True)."""
    def run():
        _pump(fw, socket.create_connection(("127.0.0.1", port), 5.0))

    threading.Thread(target=run, daemon=True).start()


# ---- dialling out to a board in listen mode ------------------------------------


def test_tcp_transport_handshake_and_command():
    fw = FakeFirmware(password=PASSWORD, mac="24a160aabbcc")
    server = FakeBoardServer(fw)
    try:
        with Bridge(host="127.0.0.1", tcp_port=server.port,
                    password=PASSWORD) as esp:
            assert fw.authed, "a network link must authenticate before commands"
            assert esp.info.mac == "24:a1:60:aa:bb:cc"
            esp.gpio.mode(2, "output")
            esp.gpio.write(2, 1)
            assert fw.gpio_levels[2] == 1
            assert esp.ping() >= 0
    finally:
        server.close()


def test_tcp_transport_rejects_wrong_password():
    fw = FakeFirmware(password=PASSWORD)
    server = FakeBoardServer(fw)
    try:
        with pytest.raises(AuthError):
            Bridge(host="127.0.0.1", tcp_port=server.port, password="wrong")
    finally:
        server.close()


# ---- boards dialling home: Bridge(wifi=True) -----------------------------------


def test_plural_wifi_collects_boards_as_ordinary_bridges():
    with Bridge(wifi=True, tcp_port=0, password=PASSWORD) as boards:
        fws = [FakeFirmware(password=PASSWORD, mac=f"24a16000000{i}",
                            name=f"b{i}") for i in range(1, 4)]
        for fw in fws:
            dial_home(fw, boards._listener.port)
        assert boards.wait_for(3, timeout=10) == 3

        # Every entry is a real Bridge: sub-APIs, not a parallel API.
        assert all(isinstance(b, Bridge) for b in boards)
        assert boards["b2"].info.mac == "24:a1:60:00:00:02"
        assert boards["24a160000002"] is boards["b2"]   # name or MAC, one index
        assert sorted(boards.idents()) == ["b1", "b2", "b3"]

        def light(esp):
            esp.gpio.mode(5, "output")
            esp.gpio.write(5, 1)
            return "done"

        results = boards.each(light)
        assert set(results) == {"b1", "b2", "b3"}   # keyed by ident, like boards[...]
        assert set(results.values()) == {"done"}
        assert all(fw.gpio_levels[5] == 1 for fw in fws)


def test_plural_wifi_rejects_a_bad_password():
    with Bridge(wifi=True, tcp_port=0, password="the-real-one") as boards:
        dial_home(FakeFirmware(password="something-else"), boards._listener.port)
        time.sleep(0.5)
        assert list(boards) == []


def test_plural_wifi_replaces_a_reconnecting_board():
    """A board reconnects after a dropout; it must not end up in the set twice."""
    with Bridge(wifi=True, tcp_port=0, password=PASSWORD) as boards:
        port = boards._listener.port
        dial_home(FakeFirmware(password=PASSWORD, mac="24a160000009"), port)
        boards.wait_for(1, timeout=10)
        first = boards[0]

        dial_home(FakeFirmware(password=PASSWORD, mac="24a160000009"), port)
        for _ in range(200):                     # same MAC: replaces, never adds
            if len(boards) == 1 and boards[0] is not first:
                break
            time.sleep(0.02)
        assert len(boards) == 1
        assert boards[0] is not first


def test_each_reports_per_board_failures():
    """One unhappy board must not fail the whole sweep — with a thousand of
    them, some are always mid-reboot."""
    with Bridge(wifi=True, tcp_port=0, password=PASSWORD) as boards:
        port = boards._listener.port
        dial_home(FakeFirmware(password=PASSWORD, mac="24a160000011"), port)
        mute = FakeFirmware(password=PASSWORD, mac="24a160000012")
        dial_home(mute, port)
        boards.wait_for(2, timeout=10)
        for b in boards:
            b.timeout = 0.3

        mute.transport.inject = lambda data: None   # stops answering entirely
        results = boards.each(lambda esp: esp.ping())
        assert isinstance(results["24:a1:60:00:00:11"], float)
        assert isinstance(results["24:a1:60:00:00:12"], BridgeTimeoutError)


def test_wait_for_times_out_when_nobody_dials_in():
    with Bridge(wifi=True, tcp_port=0, password=PASSWORD) as boards:
        with pytest.raises(BridgeTimeoutError):
            boards.wait_for(1, timeout=0.2)
