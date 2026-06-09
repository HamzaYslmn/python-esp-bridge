"""IR NEC codec: encode/decode roundtrip + receiver plumbing."""
from espbridge.drivers.ir import IrReceiver, IrSender, decode_nec, nec_symbols


def test_nec_roundtrip():
    syms = nec_symbols(addr=0x04, command=0x45)
    assert decode_nec(syms) == (0x04, 0x45)


def test_nec_roundtrip_all_bits():
    for addr, cmd in [(0x00, 0x00), (0xFF, 0xFF), (0x5A, 0xA5)]:
        assert decode_nec(nec_symbols(addr, cmd)) == (addr, cmd)


def test_decode_tolerates_jitter_and_inverted_levels():
    # Typical IR receiver modules (e.g. TSOP1738) invert the signal and
    # introduce roughly 10% timing jitter.  The decoder must handle both.
    syms = [(1 - lv, round(dur * 1.1)) for lv, dur in nec_symbols(0x10, 0x2A)]
    assert decode_nec(syms) == (0x10, 0x2A)


def test_decode_repeat_code():
    assert decode_nec([(1, 9000), (0, 2250), (1, 560)]) == "repeat"


def test_decode_rejects_garbage():
    assert decode_nec([(1, 100), (0, 100)]) is None
    bad = nec_symbols(1, 2)
    bad[10] = (bad[10][0], 3000)  # corrupt a mark
    assert decode_nec(bad) is None


def test_extended_nec_address():
    # In Extended NEC the address and its complement are different (the
    # addr/~addr consistency check fails), so the decoder returns the full
    # 16-bit address instead of the 8-bit one.
    syms = nec_symbols(0, 0x45)
    value = 0x12 | (0x34 << 8) | (0x45 << 16) | ((~0x45 & 0xFF) << 24)
    ext = [(1, 9000), (0, 4500)]
    for i in range(32):
        ext.append((1, 560))
        ext.append((0, 1690 if value >> i & 1 else 560))
    ext.append((1, 560))
    assert decode_nec(ext) == (0x3412, 0x45)
    assert syms is not ext


def test_sender_via_bridge(bridge, fw):
    tx = IrSender(bridge, pin=4)
    assert fw.rmt_carrier[4] == (38_000, 33, 1)
    tx.send_nec(0x04, 0x45)
    pin, syms = fw.rmt_tx[-1]
    assert pin == 4 and decode_nec(syms) == (0x04, 0x45)


def test_receiver_via_bridge(bridge, fw):
    rx = IrReceiver(bridge, pin=15)
    fw.rmt_capture = nec_symbols(0x01, 0x02)
    assert rx.receive(timeout_ms=10) == (0x01, 0x02)
