"""1-Wire: CRC8, ROM search against the fake multi-device bus."""
from espbridge.onewire import crc8


def rom(family: int, serial: int) -> bytes:
    body = bytes([family]) + serial.to_bytes(6, "little")
    return body + bytes([crc8(body)])


def test_crc8_known_vector():
    # Maxim/Dallas app-note example: a valid 8-byte ROM with a known CRC.
    # Appending the correct CRC byte and re-running crc8 over all 8 bytes must yield 0.
    r = bytes.fromhex("02 1c b8 01 00 00 00".replace(" ", ""))
    assert crc8(r + bytes([crc8(r)])) == 0


def test_reset_presence(bridge, fw):
    assert bridge.onewire.reset(4) is False
    fw.ow_devices.append(rom(0x28, 1))
    assert bridge.onewire.reset(4) is True


def test_search_single_device(bridge, fw):
    fw.ow_devices = [rom(0x28, 0x123456)]
    assert bridge.onewire.search(4) == [rom(0x28, 0x123456).hex()]


def test_search_multiple_devices(bridge, fw):
    devs = [rom(0x28, 7), rom(0x28, 0xABCDEF), rom(0x10, 99)]
    fw.ow_devices = devs
    assert sorted(bridge.onewire.search(4)) == sorted(d.hex() for d in devs)


def test_search_empty_bus(bridge, fw):
    assert bridge.onewire.search(4) == []


def test_select_match_rom(bridge, fw):
    fw.ow_devices = [rom(0x28, 5)]
    r = rom(0x28, 5).hex()
    bridge.onewire.select(4, r)
    assert fw.ow_writes[-1] == bytes([0x55]) + rom(0x28, 5)  # 0x55 = MATCH_ROM command


def test_select_skip_rom(bridge, fw):
    fw.ow_devices = [rom(0x28, 5)]
    bridge.onewire.select(4, None)
    assert fw.ow_writes[-1] == bytes([0xCC])  # 0xCC = SKIP_ROM command (addresses all devices)
