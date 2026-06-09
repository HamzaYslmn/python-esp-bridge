"""HC-SR04 driver: echo pulse -> distance."""
from espbridge.drivers.hcsr04 import HCSR04, echo_to_cm


def test_echo_to_cm():
    assert echo_to_cm([(1, 580)]) == 10.0
    assert echo_to_cm([(0, 30), (1, 1160), (0, 50)]) == 20.0


def test_out_of_range_returns_none():
    assert echo_to_cm([]) is None
    assert echo_to_cm([(0, 30), (1, 20)]) is None  # 20 µs pulse is too short to be a valid echo — treated as crosstalk


def test_distance_via_bridge(bridge, fw):
    sonar = HCSR04(bridge, trig=5, echo=18)
    fw.rmt_capture = [(1, 580)]
    assert sonar.distance_cm() == 10.0
    pin, idle, timeout, max_syms, trigger = fw.rmt_recv_args[0]
    assert pin == 18 and trigger == (5, 1, 10)
    assert idle == 30_000  # idle threshold of 30 ms comfortably spans the longest valid echo (~25 ms at max range)
