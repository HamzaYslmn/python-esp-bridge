"""Ethernet + camera (opt-in modules)."""
import pytest

from espbridge.camera import FRAMESIZE_SVGA


def test_eth_preset_rmii(bridge, fw):
    bridge.eth.begin("wt32-eth01")
    assert fw.eth_config == ("rmii", 1, 1, 23, 18, 16, 0)
    assert bridge.eth.wait_for_ip(timeout=2.0) == "10.0.0.9"


def test_eth_spi_w5500(bridge, fw):
    bridge.eth.begin("w5500", cs=5, irq=4, rst=14)
    assert fw.eth_config == ("spi", 17, 1, 5, 4, 14, -1, -1, -1, 20)


def test_eth_status(bridge, fw):
    bridge.eth.begin("wt32-eth01")
    st = bridge.eth.status()
    assert st["link"] is True and st["ip"] == "10.0.0.9"
    assert st["netmask"] == "255.255.255.0"


def test_camera_capture_chunks(bridge, fw):
    bridge.camera.begin("ai-thinker")
    assert fw.cam_config is not None and len(fw.cam_config) == 20
    fw.cam_frame = b"\xff\xd8" + bytes(5000) + b"\xff\xd9"  # 5004 B "JPEG"
    jpeg = bridge.camera.capture()
    assert jpeg == fw.cam_frame
    assert fw.cam_released


def test_camera_set_props(bridge, fw):
    bridge.camera.begin("xiao-s3-sense", framesize=FRAMESIZE_SVGA, quality=10)
    assert fw.cam_config[17] == FRAMESIZE_SVGA
    assert fw.cam_config[18] == 10
    bridge.camera.set("vflip", 1)
    bridge.camera.set("brightness", -2)
    assert fw.cam_props == {5: 1, 2: -2}


def test_camera_bad_pin_map(bridge):
    with pytest.raises(ValueError):
        bridge.camera.begin([1, 2, 3])
