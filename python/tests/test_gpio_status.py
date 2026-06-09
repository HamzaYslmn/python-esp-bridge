"""Gpio.status(): full pin state read back from the chip (over fake firmware)."""


def test_status_reports_mode_and_level(bridge):
    bridge.gpio.mode(2, "output")
    bridge.gpio.write(2, 1)
    st = bridge.gpio.status(2)
    assert st.pin == 2
    assert st.level == 1
    assert st.mode == "output"
    assert st.is_output is True
    assert st.pwm is False and st.pwm_freq == 0 and st.pwm_duty == 0  # no PWM channel attached to this pin


def test_status_reports_pwm(bridge):
    bridge.pwm.attach(5, freq=2000, resolution_bits=10)
    bridge.pwm.write(5, 512)
    st = bridge.gpio.status(5)
    assert st.pwm is True
    assert st.pwm_freq == 2000
    assert st.pwm_duty == 512


def test_status_unknown_mode_for_unconfigured_pin(bridge):
    st = bridge.gpio.status(9)
    assert st.mode is None
    assert st.is_output is False


def test_dump_lists_active_pins(bridge):
    bridge.gpio.mode(2, "output")
    bridge.gpio.write(2, 1)
    bridge.pwm.attach(5, freq=1500, resolution_bits=10)
    bridge.pwm.write(5, 256)
    dump = {s.pin: s for s in bridge.gpio.dump()}
    assert set(dump) == {2, 5}                 # dump only returns pins that have been configured or have PWM attached
    assert dump[2].mode == "output" and dump[2].level == 1
    assert dump[5].pwm and dump[5].pwm_freq == 1500 and dump[5].pwm_duty == 256
