"""Wi-Fi + NET socket-proxy semantics over the fake firmware."""
import struct

import pytest

from espbridge import constants as C
from espbridge.errors import BridgeTimeoutError, RemoteError


@pytest.fixture()
def wifi_up(bridge):
    st = bridge.wifi.connect("HomeWifi", "secret")
    assert st.connected and st.ip == "192.168.1.50"
    return bridge


def test_wifi_scan_collects_events(bridge):
    nets = bridge.wifi.scan(timeout=5.0)
    assert [n.ssid for n in nets] == ["HomeWifi", "Neighbor"]  # sorted by RSSI
    assert nets[0].rssi == -40 and nets[0].auth == "wpa2_psk"


def test_wifi_status_fields(wifi_up):
    st = wifi_up.wifi.status()
    assert st.gateway == "192.168.1.1"
    assert st.netmask == "255.255.255.0"
    assert st.rssi == -55
    assert st.channel == 6
    assert st.mac == "24:a1:60:12:34:56"


def test_tcp_connect_requires_wifi(bridge):
    with pytest.raises(RemoteError):
        bridge.net.tcp_connect("example.com", 80, timeout=1.0)


def test_tcp_send_recv_window_ack(wifi_up, fw):
    sock = wifi_up.net.tcp_connect("example.com", 80)
    sock.send(b"GET / HTTP/1.0\r\n\r\n")
    assert fw.tcp_sent[sock.handle] == b"GET / HTTP/1.0\r\n\r\n"

    fw.emit(C.NET_DATA_EVT, bytes([sock.handle]) + b"HTTP/1.0 200 OK\r\n")
    data = sock.recv(timeout=2.0)
    assert data == b"HTTP/1.0 200 OK\r\n"
    # consuming data must replenish the firmware's credit window
    deadline_acks = [(sock.handle, len(data))]
    for _ in range(100):
        if fw.window_acks == deadline_acks:
            break
        import time
        time.sleep(0.01)
    assert fw.window_acks == deadline_acks

    sock.close()
    assert sock.handle in fw.closed_handles


def test_tcp_recv_returns_empty_after_remote_close(wifi_up, fw):
    sock = wifi_up.net.tcp_connect("example.com", 80)
    fw.emit(C.NET_DATA_EVT, bytes([sock.handle]) + b"tail")
    fw.emit(C.NET_CLOSED_EVT, bytes([sock.handle, 0]))
    assert sock.recv(timeout=2.0) == b"tail"   # buffered data still readable
    assert sock.recv(timeout=2.0) == b""       # then EOF
    assert not sock.connected


def test_tcp_recv_timeout(wifi_up):
    sock = wifi_up.net.tcp_connect("example.com", 80)
    with pytest.raises(BridgeTimeoutError):
        sock.recv(timeout=0.2)


def test_tcp_server_accept(wifi_up, fw):
    srv = wifi_up.net.tcp_listen(8080)
    fw.tcp_sent[9] = b""  # firmware allocates handle 9 for the accepted client
    fw.emit(C.NET_ACCEPT_EVT,
            bytes([srv.handle, 9]) + bytes([10, 0, 0, 7]) + struct.pack(">H", 51234))
    conn = srv.accept(timeout=2.0)
    assert conn.handle == 9 and conn.peer == ("10.0.0.7", 51234)
    fw.emit(C.NET_DATA_EVT, bytes([9]) + b"hello")
    assert conn.recv(timeout=2.0) == b"hello"
    conn.send(b"world")
    assert fw.tcp_sent[9] == b"world"


def test_udp_roundtrip(wifi_up, fw):
    sock = wifi_up.net.udp(5005)
    sock.sendto(b"ping", ("192.168.1.99", 6000))
    assert fw.udp_sent == [(sock.handle, "192.168.1.99", 6000, b"ping")]
    fw.emit(C.NET_UDP_EVT,
            bytes([sock.handle]) + bytes([192, 168, 1, 99]) + struct.pack(">H", 6000) + b"pong")
    data, addr = sock.recvfrom(timeout=2.0)
    assert data == b"pong" and addr == ("192.168.1.99", 6000)
