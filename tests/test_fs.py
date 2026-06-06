"""Filesystem: mount, file round-trips, chunking, listing."""
import pytest

from espbridge.errors import RemoteError


def test_mount_reports_df(bridge, fw):
    vol = bridge.fs.mount("littlefs")
    assert (vol.total_kb, vol.used_kb) == (1024, 16)
    assert 0 in fw.fs_mounted


def test_write_read_roundtrip(bridge):
    vol = bridge.fs.mount("littlefs")
    vol.write_file("/hello.txt", b"hi there")
    assert vol.read_file("/hello.txt") == b"hi there"


def test_large_file_chunks(bridge):
    vol = bridge.fs.mount("littlefs")
    data = bytes(range(256)) * 40  # 10240 B > one 2040 B chunk
    vol.write_file("/big.bin", data)
    assert vol.read_file("/big.bin") == data


def test_append_and_seek(bridge):
    vol = bridge.fs.mount("littlefs")
    vol.write_file("/log.txt", b"one")
    vol.append_file("/log.txt", b"two")
    with vol.open("/log.txt") as f:
        assert f.read() == b"onetwo"
        f.seek(3)
        assert f.read() == b"two"


def test_open_missing_raises(bridge):
    vol = bridge.fs.mount("littlefs")
    with pytest.raises(RemoteError):
        vol.open("/absent.txt")


def test_list_collects_events(bridge):
    vol = bridge.fs.mount("littlefs")
    vol.write_file("/a.txt", b"123")
    vol.write_file("/b.txt", b"4567")
    entries = sorted(vol.list("/"))
    assert entries == [("a.txt", 3, False), ("b.txt", 4, False)]


def test_remove_rename_stat(bridge):
    vol = bridge.fs.mount("littlefs")
    vol.write_file("/x.txt", b"abc")
    assert vol.stat("/x.txt")[0] == 3
    vol.rename("/x.txt", "/y.txt")
    assert vol.read_file("/y.txt") == b"abc"
    vol.remove("/y.txt")
    with pytest.raises(RemoteError):
        vol.stat("/y.txt")


def test_relative_path_rejected(bridge):
    vol = bridge.fs.mount("littlefs")
    with pytest.raises(ValueError):
        vol.read_file("oops.txt")
