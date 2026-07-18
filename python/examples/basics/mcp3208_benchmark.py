"""MCP3208 (12-bit, 8-ch SPI ADC): read a channel, then benchmark sample rate.

Answers "how fast can I sample analog over the bridge?" with a number measured
on YOUR link, not a datasheet guess. The COBS/CRC framing costs microseconds;
the real limit is the USB-serial round trip per sample.

Two ways to run without a real MCP3208 wired up:
  * Jumper MOSI (GPIO23) -> MISO (GPIO19). The bus echoes the command bytes, so
    the decoded "reading" is garbage BUT the timing is 100% real — this is the
    honest throughput test.
  * No jumper: reads float, timing still real.

With a real MCP3208 wired (CS=GPIO5), the readings are real too.

Run:  uv run python mcp3208_benchmark.py [PORT]
      (pass a port like COM3 to force USB — auto-detect may pick a slow BLE link)
"""
import sys
import threading
import time

from espbridge import Bridge

CS = 5
FREQ = 1_000_000        # MCP3208 ceiling is ~1 MHz at 3.3 V (2 MHz only at 5 V)
SECS = 2.0              # measurement window per configuration


def rate(esp, workers):
    """Aggregate samples/s with `workers` threads hammering CH0 for SECS.

    The bridge is thread-safe and pipelined, so N threads overlap the USB
    round-trip latency instead of each paying it in series.
    """
    tx = bytes([0x06, 0x00, 0x00])   # MCP3208 CH0 single-ended command frame
    counts = []

    def loop():
        c, t0 = 0, time.perf_counter()
        while time.perf_counter() - t0 < SECS:
            esp.spi.transfer(tx, cs=CS)
            c += 1
        counts.append(c)

    ths = [threading.Thread(target=loop) for _ in range(workers)]
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return sum(counts) / (time.perf_counter() - t0)


with Bridge(port=sys.argv[1] if len(sys.argv) > 1 else None) as esp:
    print(f"board: {esp.info.name or '?'}  mac={esp.info.mac}  fw={esp.info.fw_version}")
    esp.spi.init(sck=18, miso=19, mosi=23, freq=FREQ, mode=0)
    adc = esp.mcp3208(cs=CS, vref=3.3)

    echo = esp.spi.transfer(b"\xDE\xAD\xBE", cs=CS)
    print("loopback:", echo.hex(),
          "(MOSI->MISO jumper OK)" if echo == b"\xDE\xAD\xBE" else "(floating / no jumper)")
    print("CH0 sample:", adc.read(0), "=", f"{adc.read_voltage(0):.3f} V "
          "(meaningful only with a real chip; echo/float otherwise)\n")

    single = rate(esp, 1)
    print(f"single-shot (1 thread) : {single:7.0f} samples/s  ({1e3 / single:.2f} ms each)")
    for w in (4, 8):
        print(f"pipelined  ({w} threads) : {rate(esp, w):7.0f} samples/s")

    print(f"\n=> {single:.0f} Hz single-shot; the per-sample floor is the USB-serial\n"
          "   latency timer, not baud. Need more? Use threads (above): the link\n"
          "   pipelines. Or push periodic sampling on-device (firmware).")
    esp.spi.deinit()
