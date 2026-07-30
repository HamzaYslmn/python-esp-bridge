"""Every board on the Wi-Fi, however many there are.

One call, because from up here there is no difference between one board, many
boards, and a Wi-Fi link:

  * boards left in listen mode answer a UDP broadcast and get dialled;
  * boards provisioned to dial home connect in and keep arriving — after a
    reboot, a dropout or a server restart, on their own.

Provision each board once, over USB or an existing Wi-Fi link:

    esp.wifi.link_setup("ssid", "password")                    # listen mode
    esp.wifi.link_setup("ssid", "password", server="10.0.0.5")  # dials home

...or bake it into the sketch with ``EspBridge.wifi.begin("ssid", "password")``
(add a third argument to dial home). Then:

    uv run python many_boards.py

Every board here is an ordinary Bridge, so `esp.gpio`, `esp.i2c`, drivers and
`esp.watch` all work exactly as they do over USB.
"""
import time

import espbridge

with espbridge.Bridge.all(
        wifi=True, on_connect=lambda esp: print(f"+ {esp.info.ident}")) as boards:
    print("waiting for boards (Ctrl-C to stop)")
    boards.wait_for(1, timeout=None)
    time.sleep(1.0)                      # let a few more arrive before the first sweep

    def blink(esp):
        esp.gpio.mode(2, "output")
        esp.gpio.write(2, 1)
        time.sleep(0.1)
        esp.gpio.write(2, 0)

    while True:
        t0 = time.perf_counter()
        pings = boards.each(lambda esp: esp.ping())
        dt = time.perf_counter() - t0
        ok = [v for v in pings.values() if isinstance(v, float)]
        print(f"pinged {len(ok)}/{len(pings)} boards in {dt * 1000:.0f} ms "
              f"({len(pings) / dt:.0f} boards/s)")

        failed = {m: r for m, r in boards.each(blink).items()
                  if isinstance(r, Exception)}
        if failed:
            print(f"  {len(failed)} board(s) failed:",
                  ", ".join(f"{m} ({type(e).__name__})"
                            for m, e in list(failed.items())[:3]))
        time.sleep(2)
