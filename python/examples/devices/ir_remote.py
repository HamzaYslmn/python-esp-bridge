"""IR remote: print NEC codes from a TSOP receiver on GPIO 15, and
re-send the last code out an IR LED on GPIO 4 when you press Enter."""
from espbridge import Bridge
from espbridge.drivers.ir import IrReceiver, IrSender

with Bridge() as esp:
    rx = IrReceiver(esp, pin=15)
    tx = IrSender(esp, pin=4)
    last = None
    print("point a remote at the receiver (Ctrl+C to quit)...")
    while True:
        code = rx.receive(timeout_ms=10_000)
        if code == "repeat":
            print("  (held)")
        elif code:
            last = code
            print(f"addr=0x{code[0]:02X} cmd=0x{code[1]:02X}")
            if input("  Enter = replay, anything else = keep listening: ") == "":
                tx.send_nec(*last)
