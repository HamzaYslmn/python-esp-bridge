"""Discover and drive every attached ESP32 bridge.

From the shell, search all attached devices first:

    espbridge ports     # serial ports only (no connection)
    espbridge scan      # connect to each: port, name, chip, MAC, firmware

One-time setup — give each board a persistent name (stored in its flash):

    espbridge -p COM7 set-name sensors
    espbridge -p COM8 set-name relays
"""
import espbridge

# Search all devices: connect_all() opens every bridge that's plugged in.
with espbridge.connect_all() as boards:
    print(f"found {len(boards)} board(s):")
    for esp in boards:
        info = esp.info
        fw = ".".join(map(str, info.fw_version))
        print(f"  {info.name or '(unnamed)':<16s} {info.chip.name:<10s} "
              f"{info.mac}  fw v{fw}")

    # Index by stored name to address a specific board.
    by_name = {esp.info.name: esp for esp in boards if esp.info.name}

    if "relays" in by_name:
        relays = by_name["relays"]
        relays.gpio.mode(2, "output")
        relays.gpio.write(2, 1)
        print("relays: GPIO2 on")
        relays.gpio.write(2, 0)

    if "sensors" in by_name:
        sensors = by_name["sensors"]
        sensors.adc.config(34, atten=11)
        print("light sensor:", sensors.adc.read_mv(34), "mV")

# To open just one specific board, address it by name (or mac="aa:bb:..."):
#     with espbridge.Bridge(name="relays") as esp:
#         esp.gpio.write(2, 1)
