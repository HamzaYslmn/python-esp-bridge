"""Discover and drive every attached ESP32 bridge.

From the shell, search all attached devices first:

    espbridge ports     # serial ports only (no connection)
    espbridge scan      # connect to each: port, name, MAC, chip, firmware

One-time setup — give each board a name (stored in its flash, max 16 chars so it
fits the Bluetooth advertisement):

    espbridge -p COM7 set-name sensors
    espbridge -p COM8 set-name relays
"""
import espbridge

# No selector, so Bridge() opens every bridge that's plugged in.
with espbridge.Bridge() as boards:
    print(f"found {len(boards)} board(s):")
    for esp in boards:
        info = esp.info
        fw = ".".join(map(str, info.fw_version))
        print(f"  {info.ident:<14s} {info.chip.name:<10s} {info.mac}  fw v{fw}")

    # Index a set by name (or MAC — same index) to address one specific board.
    if "relays" in boards.idents():
        relays = boards["relays"]
        relays.gpio.mode(2, "output")
        relays.gpio.write(2, 1)
        print("relays: GPIO2 on")
        relays.gpio.write(2, 0)

    if "sensors" in boards.idents():
        sensors = boards["sensors"]
        sensors.adc.config(34, atten=11)
        print("light sensor:", sensors.adc.read_mv(34), "mV")

# To open specific boards and nothing else, name them:
#     with espbridge.Bridge("relays") as esp:                  # one
#     with espbridge.Bridge(["relays", "sensors"]) as boards:  # several
