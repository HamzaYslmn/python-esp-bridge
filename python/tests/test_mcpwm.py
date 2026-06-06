"""MCPWM complementary pair."""


def test_begin_pair(bridge, fw):
    bridge.mcpwm.begin(25, 26, freq=20_000, deadtime_ns=500)
    assert fw.mcpwm_config == (25, 26, 20_000, 500)


def test_begin_single(bridge, fw):
    bridge.mcpwm.begin(25)
    assert fw.mcpwm_config == (25, -1, 20_000, 500)


def test_duty_permille_and_clamp(bridge, fw):
    bridge.mcpwm.begin(25, 26)
    bridge.mcpwm.duty(35.5)
    assert fw.mcpwm_duty == 355
    bridge.mcpwm.duty(150)
    assert fw.mcpwm_duty == 1000


def test_stop(bridge, fw):
    bridge.mcpwm.begin(25, 26)
    bridge.mcpwm.stop()
    assert fw.mcpwm_config is None
