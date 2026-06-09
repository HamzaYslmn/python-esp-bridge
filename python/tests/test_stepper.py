"""Stepper driver: pulse generation, ramps, direction, position."""
from espbridge.stepper import Stepper, _step_syms, ramp_periods


def test_step_syms_short_period():
    assert _step_syms(1000) == [(1, 10), (0, 990)]


def test_step_syms_splits_long_low():
    syms = _step_syms(70_000)
    assert syms[0] == (1, 10)
    assert sum(d for _, d in syms) == 70_000
    assert all(d <= 0x7FFF for _, d in syms)


def test_ramp_constant_speed():
    assert ramp_periods(3, 1000, 0) == [1000, 1000, 1000]


def test_ramp_trapezoid_symmetric():
    # accel=12800 steps/s², cruise=800 steps/s → ramp distance = v²/(2a) = 25 steps
    p = ramp_periods(100, 800, 12_800)
    assert p[0] > p[50] and p[-1] > p[50]  # ends are slow (long period), middle is fast
    assert min(p) == round(1e6 / 800)       # period at cruise speed (µs per step)
    assert p[0] == p[-1]                    # acceleration and deceleration ramps are mirror images


def test_move_counts_steps_and_sets_dir(bridge, fw):
    m = Stepper(bridge, step_pin=12, dir_pin=14)
    m.move(20, speed=1000)
    assert m.position == 20
    assert fw.gpio_levels[14] == 1
    pulses = sum(1 for _, syms in fw.rmt_tx for lv, _ in syms if lv == 1)
    assert pulses == 20
    m.move(-5, speed=1000)
    assert m.position == 15
    assert fw.gpio_levels[14] == 0


def test_long_move_chunks(bridge, fw):
    m = Stepper(bridge, step_pin=12)
    m.move(1200, speed=2000)
    assert len(fw.rmt_tx) > 1  # 1200 steps exceeds one RMT_TX payload, so it must be split across multiple requests
    pulses = sum(1 for _, syms in fw.rmt_tx for lv, _ in syms if lv == 1)
    assert pulses == 1200


def test_run_and_stop(bridge, fw):
    m = Stepper(bridge, step_pin=12, dir_pin=14)
    m.run(500)
    assert fw.rmt_loops[12] == [(1, 10), (0, 1990)]
    m.stop()
    assert 12 not in fw.rmt_loops
