"""DS18B20 driver: scratchpad decode + full read flow over the fake bus."""
import pytest

from espbridge.drivers.ds18b20 import DS18B20, parse_scratchpad
from espbridge.onewire import crc8

from test_onewire import rom


def scratchpad(raw: int) -> bytes:
    body = raw.to_bytes(2, "little", signed=True) + bytes([0x4B, 0x46, 0x7F, 0xFF, 0x0C, 0x10])
    return body + bytes([crc8(body)])


def test_parse_scratchpad():
    assert parse_scratchpad(scratchpad(0x0191), 0x28) == 25.0625  # 0x0191 = 401 → 401/16 = 25.0625 °C
    assert parse_scratchpad(scratchpad(-160), 0x28) == -10.0    # negative two's-complement: -160/16 = -10.0 °C
    assert parse_scratchpad(scratchpad(50), 0x10) == 25.0       # DS18S20 (family 0x10) divides the raw value by 2


def test_bad_crc_raises():
    with pytest.raises(ValueError):
        parse_scratchpad(scratchpad(100)[:-1] + b"\x00", 0x28)


def test_read_full_flow(bridge, fw):
    fw.ow_devices = [rom(0x28, 42)]
    probe = DS18B20(bridge, pin=4)
    probe._resolution = 9  # force 9-bit resolution so the conversion wait is only ~94 ms, not 750 ms
    assert probe.rom == rom(0x28, 42).hex()
    fw.ow_read_data = scratchpad(0x0155)  # 0x0155 = 341 → 341/16 = 21.3125 °C
    assert probe.read() == 21.3125
    # 0x44 = CONVERT_T command; it must be present to confirm a conversion was requested
    assert bytes([0x44]) in fw.ow_writes


def test_no_sensor_raises(bridge, fw):
    with pytest.raises(OSError):
        DS18B20(bridge, pin=4)


def test_discover_filters_families(bridge, fw):
    fw.ow_devices = [rom(0x28, 1), rom(0x01, 2)]  # family 0x01 is a DS2401 (not a thermometer)
    assert DS18B20.discover(bridge, 4) == [rom(0x28, 1).hex()]  # only family 0x28 devices are returned
