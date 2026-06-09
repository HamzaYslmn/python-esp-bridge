"""asyncio: await bridge calls and fan out concurrent I/O.

The firmware already runs requests concurrently (see rtos_concurrency.py);
AsyncBridge exposes that to asyncio so you can `await` and use asyncio.gather
instead of managing threads. Calls awaited together overlap on the wire.

    uv run basics/async_fanout.py

Same API as Bridge — just `await` the calls, inside `async with`.
"""
import asyncio
import time

from espbridge import AsyncBridge

LED = 2
ADC_PINS = [34, 35, 36, 39]


async def main() -> None:
    async with AsyncBridge(ble=False) as esp:
        info = esp.info  # plain attributes pass through (no await needed)
        print(f"connected to {info.chip.name} ({info.name or info.mac})\n")

        await esp.gpio.mode(LED, "output")
        await esp.gpio.write(LED, 1)

        for pin in ADC_PINS:
            await esp.adc.config(pin, atten=11)

        # --- sequential: one await after another (N round-trips back to back) ---
        t0 = time.perf_counter()
        seq = [await esp.adc.read(pin) for pin in ADC_PINS]
        seq_ms = (time.perf_counter() - t0) * 1000

        # --- concurrent: all issued together, replies overlap on the link ------
        t0 = time.perf_counter()
        par = await asyncio.gather(*(esp.adc.read(pin) for pin in ADC_PINS))
        par_ms = (time.perf_counter() - t0) * 1000

        print(f"sequential reads {seq}  in {seq_ms:5.1f} ms")
        print(f"concurrent reads {par}  in {par_ms:5.1f} ms")
        print(f"\nfanning out {len(ADC_PINS)} reads with gather() overlapped them "
              f"on the wire ({seq_ms / par_ms:.1f}x faster here).")

        await esp.gpio.write(LED, 0)


if __name__ == "__main__":
    asyncio.run(main())
