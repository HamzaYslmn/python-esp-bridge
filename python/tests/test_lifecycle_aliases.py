"""The begin()/end() house-style aliases must mirror the real lifecycle methods.

Peripherals historically used six different verb pairs (init/deinit, mount/umount,
begin/stop, ...). begin()/end() now works everywhere via additive aliases; the
original names keep working too. This locks the alias targets so they can't drift.
"""
from espbridge.fs import Fs, Volume
from espbridge.i2c import I2c
from espbridge.mcpwm import Mcpwm
from espbridge.spi import Spi
from espbridge.uart import Uart


def test_begin_end_aliases_point_to_real_methods():
    assert I2c.begin is I2c.init
    assert I2c.end is I2c.deinit
    assert Spi.begin is Spi.init
    assert Spi.end is Spi.deinit
    assert Uart.begin is Uart.init
    assert Mcpwm.end is Mcpwm.stop
    assert Fs.begin is Fs.mount
    assert Volume.end is Volume.umount
