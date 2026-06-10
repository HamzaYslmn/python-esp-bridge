"""Peripheral tool groups for the espbridge MCP server.

Each ``register_*`` function defines tools that drive one area of the Bridge
API. Tools are thin wrappers: they fetch the live Bridge from the manager,
call the corresponding ``esp.<subapi>.<method>(...)``, and return JSON-friendly
values (bytes in/out as hex strings — see common.to_bytes/hex_str).
"""
from __future__ import annotations

from ..errors import BridgeError, BridgeTimeoutError, UnsupportedError
from .common import guarded, hex_str, info_dict, to_bytes


def register_all(mcp, mgr) -> None:
    register_system(mcp, mgr)
    register_gpio(mcp, mgr)
    register_analog(mcp, mgr)
    register_pwm(mcp, mgr)
    register_i2c(mcp, mgr)
    register_spi(mcp, mgr)
    register_uart(mcp, mgr)
    register_wifi(mcp, mgr)
    register_nvs(mcp, mgr)
    register_fs(mcp, mgr)
    register_onewire(mcp, mgr)
    register_espnow(mcp, mgr)
    register_can(mcp, mgr)
    register_mcpwm(mcp, mgr)
    register_eth(mcp, mgr)
    register_camera(mcp, mgr)
    register_ota(mcp, mgr)


# ---- system ------------------------------------------------------------------

def register_system(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def system_info() -> dict:
        """Chip model, MAC, firmware version, flash size and capability list."""
        return info_dict(mgr.bridge())

    @mcp.tool
    @guarded
    def board_status() -> dict:
        """Whole-board snapshot — call this to see everything that's set up before
        acting. Returns: chip/firmware/heap; every active GPIO pin (configured or
        PWM-driven) with its mode, level and PWM freq/duty; and the I2C addresses
        that respond on each initialized bus.

        It reports the raw addresses that ACK, not what the parts are — an I2C
        address does not uniquely identify a device, so identify the actual
        hardware from your own wiring/datasheets. I2C buses appear here once
        you've called i2c_init."""
        esp = mgr.bridge()
        info = esp.info
        try:
            pins = [{"pin": s.pin, "mode": s.mode, "level": s.level, "pwm": s.pwm,
                     "pwm_freq": s.pwm_freq, "pwm_duty": s.pwm_duty}
                    for s in esp.gpio.dump()]
            pins_note = None
        except UnsupportedError as e:
            pins, pins_note = [], str(e)  # older firmware without GPIO_DUMP
        i2c = []
        for bus, cfg in sorted(mgr.i2c_buses.items()):
            entry = {"bus": bus, **cfg}
            try:
                entry["devices"] = [{"addr": a, "hex": f"0x{a:02x}"}
                                    for a in esp.i2c.scan(bus)]
            except BridgeError as e:
                entry["error"] = str(e)
            i2c.append(entry)
        return {
            "chip": info.chip.name,
            "mac": info.mac,
            "firmware": ".".join(map(str, info.fw_version)),
            "free_heap": esp.free_heap()["free"],
            "pins": pins,
            "pins_note": pins_note,
            "i2c": i2c,
            "i2c_note": None if i2c else "no I2C bus initialized yet — call "
            "i2c_init(sda=, scl=) to scan a bus for device addresses",
        }

    @mcp.tool
    @guarded
    def system_ping() -> dict:
        """Round-trip the link; returns latency in milliseconds."""
        return {"latency_ms": round(mgr.bridge().ping() * 1000, 3)}

    @mcp.tool
    @guarded
    def system_free_heap() -> dict:
        """Heap metrics (free / min_free / largest_block) and dropped-frame counters."""
        return mgr.bridge().free_heap()

    @mcp.tool
    @guarded
    def system_reset() -> str:
        """Soft-reset the ESP32 and wait for it to reconnect."""
        mgr.bridge().reset()
        return "reset complete"

    @mcp.tool
    @guarded
    def system_set_name(name: str) -> str:
        """Persist a device name on the board (NVS) for later name-based selection."""
        mgr.bridge().set_name(name)
        return f"name set to {name!r}"

    @mcp.tool
    @guarded
    def system_deep_sleep(seconds: float = 0, wake_pin: int | None = None,
                          wake_level: int = 1) -> str:
        """Deep sleep (the board reboots on wake and the link drops). Wake on a
        timer (seconds), a GPIO level (wake_pin/wake_level), or both."""
        mgr.bridge().deep_sleep(seconds, wake_pin=wake_pin, wake_level=wake_level)
        return "entered deep sleep (board will reboot on wake; reconnect after)"

    @mcp.tool
    @guarded
    def system_light_sleep(seconds: float = 0, wake_pin: int | None = None,
                           wake_level: int = 1) -> dict:
        """Light sleep (RAM and the link survive); returns the wake cause code."""
        cause = mgr.bridge().light_sleep(seconds, wake_pin=wake_pin,
                                         wake_level=wake_level)
        return {"wake_cause": cause}

    @mcp.tool
    @guarded
    def system_wake_cause() -> dict:
        """esp_sleep_wakeup_cause_t of the last boot (0 reset, 4 timer, 7 gpio, ...)."""
        return {"wake_cause": mgr.bridge().wake_cause()}

    @mcp.tool
    @guarded
    def system_cpu_freq(mhz: int) -> dict:
        """Set the CPU clock (80/160/240 MHz; 80 = battery floor with radios on)."""
        return {"cpu_mhz": mgr.bridge().cpu_freq(mhz)}

    @mcp.tool
    @guarded
    def system_power_mode(mode: str) -> dict:
        """Apply a power profile: 'battery' (80 MHz CPU + relaxed BLE link) or
        'performance' (240 MHz + fast BLE link). ESP-NOW RX duty is separate —
        see espnow_power_save."""
        return mgr.bridge().power_mode(mode)


# ---- gpio --------------------------------------------------------------------

def register_gpio(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def gpio_mode(pin: int, mode: str) -> str:
        """Set a pin's mode: input | output | input_pullup | input_pulldown |
        output_open_drain."""
        mgr.bridge().gpio.mode(pin, mode)
        return f"GPIO{pin} mode set to {mode}"

    @mcp.tool
    @guarded
    def gpio_write(pin: int, value: int, verify: bool = False) -> dict:
        """Drive an output pin (value 0/1). Returns the level read back from the
        chip and `ok` — false means the pin did NOT reach the requested level
        (wrong mode, not an output, shorted, or driven elsewhere)."""
        want = 1 if value else 0
        level = mgr.bridge().gpio.write(pin, value, verify=verify)
        return {"pin": pin, "value": want, "level": level, "ok": level == want}

    @mcp.tool
    @guarded
    def gpio_read(pin: int) -> dict:
        """Read a pin's current level (0/1)."""
        return {"pin": pin, "level": mgr.bridge().gpio.read(pin)}

    @mcp.tool
    @guarded
    def gpio_status(pin: int) -> dict:
        """Full state of a pin from the chip: live level, configured mode
        (input/output/... or null), and whether a PWM channel drives it
        (pwm=true with pwm_freq/pwm_duty). Use this to see what's really on a
        pin, not just its digital value."""
        st = mgr.bridge().gpio.status(pin)
        return {"pin": st.pin, "level": st.level, "mode": st.mode,
                "is_output": st.is_output, "pwm": st.pwm,
                "pwm_freq": st.pwm_freq, "pwm_duty": st.pwm_duty}

    @mcp.tool
    @guarded
    def gpio_read_all() -> dict:
        """Read every pin at once; returns a bitmask (bit N = GPIO N)."""
        return {"bitmask": mgr.bridge().gpio.read_all()}

    @mcp.tool
    @guarded
    def gpio_write_many(values: dict[int, int]) -> str:
        """Set several output pins in one round-trip, e.g. {"2": 1, "4": 0}."""
        mgr.bridge().gpio.write_many({int(k): v for k, v in values.items()})
        return f"wrote {len(values)} pins"


# ---- adc / dac / touch -------------------------------------------------------

def register_analog(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def adc_config(pin: int, atten: float = 11) -> str:
        """Set ADC attenuation for a pin (0, 2.5, 6 or 11 dB; 11 dB ~ full 3.3 V)."""
        mgr.bridge().adc.config(pin, atten)
        return f"ADC on GPIO{pin} set to {atten} dB"

    @mcp.tool
    @guarded
    def adc_read(pin: int) -> dict:
        """Raw 12-bit ADC reading (0..4095) on a pin."""
        return {"pin": pin, "raw": mgr.bridge().adc.read(pin)}

    @mcp.tool
    @guarded
    def adc_read_mv(pin: int) -> dict:
        """Calibrated ADC reading in millivolts on a pin."""
        return {"pin": pin, "mv": mgr.bridge().adc.read_mv(pin)}

    @mcp.tool
    @guarded
    def dac_write(pin: int, value: int) -> str:
        """Output a DAC value 0..255 (0..3.3 V). Classic ESP32 GPIO 25/26, S2 17/18."""
        mgr.bridge().dac.write(pin, value)
        return f"DAC GPIO{pin} = {value}"

    @mcp.tool
    @guarded
    def dac_cosine(pin: int, freq_hz: int, scale: int = 0, offset: int = 0,
                   phase_180: bool = False) -> str:
        """Start the hardware cosine generator (~130 Hz..100 kHz). scale 0..3 =
        full/half/quarter/eighth amplitude."""
        mgr.bridge().dac.cosine(pin, freq_hz, scale=scale, offset=offset,
                                phase_180=phase_180)
        return f"cosine generator on GPIO{pin} at {freq_hz} Hz"

    @mcp.tool
    @guarded
    def dac_disable(pin: int) -> str:
        """Stop the cosine generator and disable the DAC output on a pin."""
        esp = mgr.bridge()
        esp.dac.cosine_stop(pin)
        esp.dac.disable(pin)
        return f"DAC GPIO{pin} disabled"

    @mcp.tool
    @guarded
    def touch_read(pin: int) -> dict:
        """Capacitive touch reading on a touch-capable pin."""
        return {"pin": pin, "value": mgr.bridge().touch.read(pin)}


# ---- pwm ---------------------------------------------------------------------

def register_pwm(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def pwm_attach(pin: int, freq: int = 1000, resolution_bits: int = 10) -> str:
        """Attach LEDC PWM to a pin at the given frequency and duty resolution."""
        mgr.bridge().pwm.attach(pin, freq, resolution_bits)
        return f"PWM attached on GPIO{pin}: {freq} Hz, {resolution_bits}-bit"

    @mcp.tool
    @guarded
    def pwm_write(pin: int, duty: int) -> str:
        """Set raw PWM duty (0 .. 2**resolution_bits - 1) on an attached pin."""
        mgr.bridge().pwm.write(pin, duty)
        return f"PWM GPIO{pin} duty = {duty}"

    @mcp.tool
    @guarded
    def pwm_duty_pct(pin: int, percent: float) -> str:
        """Set PWM duty as a percentage 0..100 (auto-attaches the pin if needed)."""
        mgr.bridge().pwm.duty_pct(pin, percent)
        return f"PWM GPIO{pin} duty = {percent}%"

    @mcp.tool
    @guarded
    def pwm_detach(pin: int) -> str:
        """Detach PWM from a pin."""
        mgr.bridge().pwm.detach(pin)
        return f"PWM detached from GPIO{pin}"

    @mcp.tool
    @guarded
    def pwm_tone(pin: int, freq: int) -> str:
        """Play a square-wave tone (0 = off) — buzzer friendly."""
        mgr.bridge().pwm.tone(pin, freq)
        return f"tone {freq} Hz on GPIO{pin}"

    @mcp.tool
    @guarded
    def pwm_servo(pin: int, angle: float, min_us: int = 500, max_us: int = 2500) -> str:
        """Drive a hobby servo: 50 Hz, angle 0..180 mapped to min_us..max_us."""
        mgr.bridge().pwm.servo(pin, angle, min_us=min_us, max_us=max_us)
        return f"servo GPIO{pin} -> {angle} deg"


# ---- i2c ---------------------------------------------------------------------

def register_i2c(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def i2c_init(sda: int = 21, scl: int = 22, freq: int = 400_000, bus: int = 0) -> str:
        """Initialize an I2C master bus (0 or 1) on the given SDA/SCL pins."""
        mgr.bridge().i2c.init(sda=sda, scl=scl, freq=freq, bus=bus)
        mgr.note_i2c(bus, sda, scl, freq)  # remember bus config so board_status can scan it
        return f"I2C bus {bus} ready (SDA={sda}, SCL={scl}, {freq} Hz)"

    @mcp.tool
    @guarded
    def i2c_scan(bus: int = 0) -> dict:
        """Scan a bus; returns the 7-bit addresses that ACK (decimal and hex)."""
        addrs = mgr.bridge().i2c.scan(bus)
        return {"addresses": addrs, "hex": [f"0x{a:02x}" for a in addrs]}

    @mcp.tool
    @guarded
    def i2c_write(addr: int, data: str, bus: int = 0) -> str:
        """Write hex bytes to a device, e.g. data="00ff"."""
        payload = to_bytes(data)
        mgr.bridge().i2c.write(addr, payload, bus)
        return f"wrote {len(payload)} bytes to 0x{addr:02x}"

    @mcp.tool
    @guarded
    def i2c_read(addr: int, n: int, bus: int = 0) -> dict:
        """Read n bytes (1..255) from a device; returns hex."""
        return {"hex": hex_str(mgr.bridge().i2c.read(addr, n, bus))}

    @mcp.tool
    @guarded
    def i2c_write_read(addr: int, wdata: str, rlen: int, bus: int = 0) -> dict:
        """Write hex bytes then read rlen bytes with a repeated start (register read)."""
        r = mgr.bridge().i2c.write_read(addr, to_bytes(wdata), rlen, bus)
        return {"hex": hex_str(r)}

    @mcp.tool
    @guarded
    def i2c_read_reg(addr: int, reg: int, n: int = 1, bus: int = 0) -> dict:
        """Read n bytes from register reg of a device; returns hex."""
        return {"hex": hex_str(mgr.bridge().i2c.read_reg(addr, reg, n, bus))}

    @mcp.tool
    @guarded
    def i2c_write_reg(addr: int, reg: int, data: str, bus: int = 0) -> str:
        """Write hex bytes to register reg of a device."""
        mgr.bridge().i2c.write_reg(addr, reg, to_bytes(data), bus)
        return f"wrote register 0x{reg:02x} on 0x{addr:02x}"

    @mcp.tool
    @guarded
    def i2c_deinit(bus: int = 0) -> str:
        """Release an I2C bus."""
        mgr.bridge().i2c.deinit(bus)
        mgr.forget_i2c(bus)
        return f"I2C bus {bus} released"


# ---- spi ---------------------------------------------------------------------

def register_spi(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def spi_init(sck: int = 18, miso: int = 19, mosi: int = 23, freq: int = 1_000_000,
                 mode: int = 0, msb_first: bool = True, host: int = 0) -> str:
        """Initialize an SPI master bus."""
        mgr.bridge().spi.init(sck=sck, miso=miso, mosi=mosi, freq=freq, mode=mode,
                              msb_first=msb_first, host=host)
        return f"SPI host {host} ready (SCK={sck}, MISO={miso}, MOSI={mosi}, {freq} Hz)"

    @mcp.tool
    @guarded
    def spi_transfer(tx: str, cs: int | None = None, host: int = 0) -> dict:
        """Full-duplex transfer of hex bytes; returns the bytes clocked in (hex).
        With cs given, that GPIO is held low for the transfer."""
        r = mgr.bridge().spi.transfer(to_bytes(tx), cs=cs, host=host)
        return {"hex": hex_str(r)}

    @mcp.tool
    @guarded
    def spi_deinit(host: int = 0) -> str:
        """Release an SPI bus."""
        mgr.bridge().spi.deinit(host)
        return f"SPI host {host} released"


# ---- uart --------------------------------------------------------------------

def register_uart(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def uart_init(port: int = 1, tx: int = 17, rx: int = 16, baud: int = 115_200) -> str:
        """Open a secondary UART (1 or 2). RX is buffered on the host for uart_read."""
        mgr.open_uart(port=port, tx=tx, rx=rx, baud=baud)
        return f"UART{port} open (TX={tx}, RX={rx}, {baud} baud)"

    @mcp.tool
    @guarded
    def uart_write(port: int, data: str, text: bool = False) -> str:
        """Write to a UART. By default data is hex; set text=true to send data as
        a UTF-8 string instead."""
        p = mgr.uart_port(port)
        if p is None:
            raise ValueError(f"UART{port} is not open — call uart_init first")
        payload = data.encode() if text else to_bytes(data)
        p.write(payload)
        return f"wrote {len(payload)} bytes to UART{port}"

    @mcp.tool
    @guarded
    def uart_read(port: int, n: int | None = None, timeout: float = 1.0) -> dict:
        """Read buffered bytes from a UART (up to n; all if omitted). Returns both
        hex and a best-effort UTF-8 decode."""
        p = mgr.uart_port(port)
        if p is None:
            raise ValueError(f"UART{port} is not open — call uart_init first")
        data = p.read(n, timeout=timeout)
        return {"hex": hex_str(data), "text": data.decode("utf-8", "replace")}

    @mcp.tool
    @guarded
    def uart_close(port: int) -> str:
        """Close a secondary UART."""
        mgr.close_uart(port)
        return f"UART{port} closed"


# ---- wifi --------------------------------------------------------------------

def register_wifi(mcp, mgr) -> None:
    def _status_dict(st) -> dict:
        return {"status": st.status, "connected": st.connected, "ip": st.ip,
                "gateway": st.gateway, "netmask": st.netmask, "rssi": st.rssi,
                "channel": st.channel, "mac": st.mac}

    @mcp.tool
    @guarded
    def wifi_scan(timeout: float = 15.0) -> list[dict]:
        """Scan for Wi-Fi networks; returns them sorted by signal strength."""
        return [{"ssid": n.ssid, "rssi": n.rssi, "bssid": n.bssid,
                 "channel": n.channel, "auth": n.auth}
                for n in mgr.bridge().wifi.scan(timeout)]

    @mcp.tool
    @guarded
    def wifi_connect(ssid: str, password: str = "", timeout: float = 20.0) -> dict:
        """Join a Wi-Fi network (station mode); returns the connection status."""
        return _status_dict(mgr.bridge().wifi.connect(ssid, password, timeout=timeout))

    @mcp.tool
    @guarded
    def wifi_disconnect() -> str:
        """Disconnect from the current Wi-Fi network."""
        mgr.bridge().wifi.disconnect()
        return "wifi disconnected"

    @mcp.tool
    @guarded
    def wifi_status() -> dict:
        """Current Wi-Fi station status (IP, gateway, RSSI, ...)."""
        return _status_dict(mgr.bridge().wifi.status())

    @mcp.tool
    @guarded
    def wifi_ap_start(ssid: str, password: str = "", channel: int = 1,
                      max_conn: int = 4) -> dict:
        """Start a Wi-Fi access point; returns its IP (clients get 192.168.4.x)."""
        return {"ip": mgr.bridge().wifi.ap_start(ssid, password, channel=channel,
                                                 max_conn=max_conn)}

    @mcp.tool
    @guarded
    def wifi_ap_stop() -> str:
        """Stop the access point."""
        mgr.bridge().wifi.ap_stop()
        return "AP stopped"


# ---- nvs ---------------------------------------------------------------------

def register_nvs(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def nvs_set_str(key: str, value: str) -> str:
        """Store a string under a key (max 15-byte key) in on-board NVS flash."""
        mgr.bridge().nvs.set(key, value)
        return f"set {key!r}"

    @mcp.tool
    @guarded
    def nvs_set_int(key: str, value: int) -> str:
        """Store a signed integer under a key in on-board NVS flash."""
        mgr.bridge().nvs.set(key, int(value))
        return f"set {key!r}"

    @mcp.tool
    @guarded
    def nvs_set_bytes(key: str, value: str) -> str:
        """Store raw hex bytes under a key in on-board NVS flash."""
        mgr.bridge().nvs.set(key, to_bytes(value))
        return f"set {key!r}"

    @mcp.tool
    @guarded
    def nvs_get_str(key: str) -> dict:
        """Read a key as a string (value null if the key is absent)."""
        return {"value": mgr.bridge().nvs.get_str(key)}

    @mcp.tool
    @guarded
    def nvs_get_int(key: str) -> dict:
        """Read a key as a signed integer (value null if the key is absent)."""
        return {"value": mgr.bridge().nvs.get_int(key)}

    @mcp.tool
    @guarded
    def nvs_get_bytes(key: str) -> dict:
        """Read a key as raw bytes, returned as hex (null if the key is absent)."""
        v = mgr.bridge().nvs.get(key)
        return {"hex": None if v is None else hex_str(v)}

    @mcp.tool
    @guarded
    def nvs_keys() -> list[str]:
        """List all keys in the user namespace."""
        return mgr.bridge().nvs.keys()

    @mcp.tool
    @guarded
    def nvs_delete(key: str) -> dict:
        """Delete a key; returns existed=false if it was not present."""
        return {"existed": mgr.bridge().nvs.delete(key)}

    @mcp.tool
    @guarded
    def nvs_clear() -> str:
        """Erase every key in the user namespace."""
        mgr.bridge().nvs.clear()
        return "NVS cleared"


# ---- filesystem --------------------------------------------------------------

def register_fs(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def fs_mount(kind: str = "littlefs", cs: int = 5, sck: int = -1, miso: int = -1,
                 mosi: int = -1, freq_mhz: int = 20) -> dict:
        """Mount a filesystem: "littlefs" (internal flash), "sd" (SPI card; give
        cs and optionally sck/miso/mosi) or "sdmmc". Returns total/used KiB."""
        vol = mgr.volume(kind, remount=True, cs=cs, sck=sck, miso=miso,
                         mosi=mosi, freq_mhz=freq_mhz)
        return {"kind": kind, "total_kb": vol.total_kb, "used_kb": vol.used_kb}

    @mcp.tool
    @guarded
    def fs_list(path: str = "/", kind: str = "littlefs") -> list[dict]:
        """List a directory; returns [{name, size, isdir}, ...]."""
        return [{"name": name, "size": size, "isdir": isdir}
                for name, size, isdir in mgr.volume(kind).list(path)]

    @mcp.tool
    @guarded
    def fs_tree(path: str = "/", kind: str = "littlefs", max_depth: int = 6,
                max_entries: int = 1000) -> dict:
        """Recursively walk a filesystem subtree so you can see everything inside
        it at once: returns [{path, size, isdir}, ...] depth-first. Bounded by
        max_depth/max_entries (truncated=true if the cap was hit)."""
        vol = mgr.volume(kind)
        out: list[dict] = []

        def walk(p: str, depth: int) -> None:
            if depth > max_depth or len(out) >= max_entries:
                return
            base = p.rstrip("/")
            for name, size, isdir in vol.list(p):
                full = f"{base}/{name}"
                out.append({"path": full, "size": size, "isdir": isdir})
                if isdir:
                    walk(full, depth + 1)

        walk(path, 0)
        return {"entries": out, "count": len(out),
                "truncated": len(out) >= max_entries}

    @mcp.tool
    @guarded
    def fs_read_text(path: str, kind: str = "littlefs") -> dict:
        """Read a file and decode it as UTF-8 text (best effort)."""
        data = mgr.volume(kind).read_file(path)
        return {"text": data.decode("utf-8", "replace"), "size": len(data)}

    @mcp.tool
    @guarded
    def fs_read_bytes(path: str, kind: str = "littlefs") -> dict:
        """Read a file and return its bytes as hex."""
        data = mgr.volume(kind).read_file(path)
        return {"hex": hex_str(data), "size": len(data)}

    @mcp.tool
    @guarded
    def fs_write_text(path: str, content: str, kind: str = "littlefs") -> str:
        """Write UTF-8 text to a file (absolute path, overwrites)."""
        mgr.volume(kind).write_file(path, content.encode())
        return f"wrote {len(content.encode())} bytes to {path}"

    @mcp.tool
    @guarded
    def fs_write_bytes(path: str, content: str, kind: str = "littlefs") -> str:
        """Write hex bytes to a file (absolute path, overwrites)."""
        data = to_bytes(content)
        mgr.volume(kind).write_file(path, data)
        return f"wrote {len(data)} bytes to {path}"

    @mcp.tool
    @guarded
    def fs_stat(path: str, kind: str = "littlefs") -> dict:
        """Stat a path; returns {size, isdir, mtime}."""
        size, isdir, mtime = mgr.volume(kind).stat(path)
        return {"size": size, "isdir": isdir, "mtime": mtime}

    @mcp.tool
    @guarded
    def fs_remove(path: str, kind: str = "littlefs") -> str:
        """Delete a file."""
        mgr.volume(kind).remove(path)
        return f"removed {path}"

    @mcp.tool
    @guarded
    def fs_mkdir(path: str, kind: str = "littlefs") -> str:
        """Create a directory."""
        mgr.volume(kind).mkdir(path)
        return f"created {path}"

    @mcp.tool
    @guarded
    def fs_rename(src: str, dst: str, kind: str = "littlefs") -> str:
        """Rename/move a file (both paths absolute)."""
        mgr.volume(kind).rename(src, dst)
        return f"renamed {src} -> {dst}"

    @mcp.tool
    @guarded
    def fs_df(kind: str = "littlefs") -> dict:
        """Free-space report for a mounted volume; returns {total_kb, used_kb}."""
        total, used = mgr.volume(kind).df()
        return {"total_kb": total, "used_kb": used}


# ---- 1-wire ------------------------------------------------------------------

def register_onewire(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def onewire_search(pin: int, alarm: bool = False) -> list[str]:
        """Enumerate 1-Wire device ROM codes (hex) on a pin."""
        return mgr.bridge().onewire.search(pin, alarm=alarm)

    @mcp.tool
    @guarded
    def onewire_reset(pin: int) -> dict:
        """Pulse a 1-Wire reset; returns present=true if any device answered."""
        return {"present": mgr.bridge().onewire.reset(pin)}

    @mcp.tool
    @guarded
    def onewire_write(pin: int, data: str, power: bool = False) -> str:
        """Write hex bytes on a 1-Wire bus. power=true holds the line driven
        high afterwards (parasite-powered conversions)."""
        payload = to_bytes(data)
        mgr.bridge().onewire.write(pin, payload, power=power)
        return f"wrote {len(payload)} bytes on 1-Wire GPIO{pin}"

    @mcp.tool
    @guarded
    def onewire_read(pin: int, n: int) -> dict:
        """Read n bytes from a 1-Wire bus; returns hex."""
        return {"hex": hex_str(mgr.bridge().onewire.read(pin, n))}


# ---- esp-now -----------------------------------------------------------------

def register_espnow(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def espnow_begin(channel: int = 0, long_range: bool = False) -> dict:
        """Start ESP-NOW; returns this board's MAC (give it to peers). channel 0
        inherits the current Wi-Fi channel."""
        return {"mac": mgr.bridge().espnow.begin(channel, long_range=long_range)}

    @mcp.tool
    @guarded
    def espnow_add_peer(mac: str, channel: int = 0) -> str:
        """Register an ESP-NOW peer by MAC address."""
        mgr.bridge().espnow.add_peer(mac, channel=channel)
        return f"peer {mac} added"

    @mcp.tool
    @guarded
    def espnow_send(mac: str, data: str) -> dict:
        """Send hex bytes (max 250) to a registered peer; returns delivered (radio ACK)."""
        return {"delivered": mgr.bridge().espnow.send(mac, to_bytes(data))}

    @mcp.tool
    @guarded
    def espnow_broadcast(data: str) -> str:
        """Broadcast hex bytes to every listening board in range (never ACKed)."""
        mgr.bridge().espnow.broadcast(to_bytes(data))
        return "broadcast sent"

    @mcp.tool
    @guarded
    def espnow_read(timeout: float = 1.0) -> dict | None:
        """Pop the oldest received ESP-NOW packet (null on timeout). Only works
        when no on-board callback is consuming packets."""
        try:
            mac, data, rssi = mgr.bridge().espnow.read(timeout=timeout)
        except BridgeTimeoutError:
            return None
        return {"mac": mac, "hex": hex_str(data), "rssi": rssi}

    @mcp.tool
    @guarded
    def espnow_power_save(window_ms: int, interval_ms: int | None = None) -> str:
        """Connectionless power save: RF listens window_ms at the start of every
        interval_ms. Default after begin is 65535 (always listening, most
        reliable); shrink for battery boards. window 0 = RX off, TX still works."""
        mgr.bridge().espnow.power_save(window_ms, interval_ms)
        return f"wake window {window_ms} ms" + (f", interval {interval_ms} ms" if interval_ms else "")

    @mcp.tool
    @guarded
    def espnow_end() -> str:
        """Stop ESP-NOW (peers are forgotten)."""
        mgr.bridge().espnow.end()
        return "ESP-NOW stopped"


# ---- can / twai --------------------------------------------------------------

def register_can(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def can_begin(tx: int, rx: int, bitrate: int = 500_000, mode: str = "normal") -> str:
        """Start the CAN (TWAI) controller. mode: normal | listen | no_ack.
        bitrate: one of 25000..1000000 standard rates."""
        mgr.bridge().can.begin(tx, rx, bitrate, mode=mode)
        return f"CAN up at {bitrate} bps ({mode})"

    @mcp.tool
    @guarded
    def can_send(id: int, data: str = "", extended: bool = False,
                 rtr: bool = False) -> str:
        """Send a CAN frame: id plus up to 8 data bytes as hex."""
        mgr.bridge().can.send(id, to_bytes(data), extended=extended, rtr=rtr)
        return f"sent CAN id 0x{id:x}"

    @mcp.tool
    @guarded
    def can_recv(timeout: float = 1.0) -> dict | None:
        """Receive the next CAN frame (null on timeout)."""
        msg = mgr.bridge().can.recv(timeout=timeout)
        if msg is None:
            return None
        return {"id": msg.id, "hex": hex_str(msg.data), "extended": msg.extended,
                "rtr": msg.rtr}

    @mcp.tool
    @guarded
    def can_status() -> dict:
        """CAN controller state and error counters."""
        return mgr.bridge().can.status()

    @mcp.tool
    @guarded
    def can_recover() -> str:
        """Start bus-off recovery."""
        mgr.bridge().can.recover()
        return "recovery started"

    @mcp.tool
    @guarded
    def can_end() -> str:
        """Stop the CAN controller."""
        mgr.bridge().can.end()
        return "CAN stopped"


# ---- mcpwm -------------------------------------------------------------------

def register_mcpwm(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def mcpwm_begin(pin_a: int, pin_b: int | None = None, freq: int = 20_000,
                    deadtime_ns: int = 500) -> str:
        """Start a complementary PWM pair with hardware deadtime (H-bridge / motor
        drivers). pin_b null = single output."""
        mgr.bridge().mcpwm.begin(pin_a, pin_b, freq=freq, deadtime_ns=deadtime_ns)
        return f"MCPWM on {pin_a}/{pin_b} at {freq} Hz"

    @mcp.tool
    @guarded
    def mcpwm_duty(percent: float) -> str:
        """Set MCPWM duty 0..100% on pin_a (pin_b runs the complement)."""
        mgr.bridge().mcpwm.duty(percent)
        return f"MCPWM duty = {percent}%"

    @mcp.tool
    @guarded
    def mcpwm_stop() -> str:
        """Stop MCPWM output."""
        mgr.bridge().mcpwm.stop()
        return "MCPWM stopped"


# ---- ethernet ----------------------------------------------------------------

def register_eth(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def eth_begin(preset_or_phy: str, addr: int | None = None, cs: int | None = None,
                  irq: int | None = None, rst: int | None = None,
                  freq_mhz: int | None = None) -> str:
        """Start Ethernet from a board preset (e.g. "wt32-eth01") or a PHY name
        (e.g. "w5500" with cs/irq/rst for an SPI module)."""
        kw = {k: v for k, v in dict(addr=addr, cs=cs, irq=irq, rst=rst,
                                    freq_mhz=freq_mhz).items() if v is not None}
        mgr.bridge().eth.begin(preset_or_phy, **kw)
        return f"Ethernet starting ({preset_or_phy})"

    @mcp.tool
    @guarded
    def eth_wait_for_ip(timeout: float = 15.0) -> dict:
        """Block until Ethernet gets a DHCP address; returns the IP."""
        return {"ip": mgr.bridge().eth.wait_for_ip(timeout)}

    @mcp.tool
    @guarded
    def eth_status() -> dict:
        """Ethernet link/IP status."""
        return mgr.bridge().eth.status()

    @mcp.tool
    @guarded
    def eth_end() -> str:
        """Stop Ethernet."""
        mgr.bridge().eth.end()
        return "Ethernet stopped"


# ---- camera ------------------------------------------------------------------

def register_camera(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def camera_begin(preset: str = "ai-thinker", framesize: int = 7, quality: int = 12,
                     xclk_mhz: int = 20, fb_count: int = 1) -> str:
        """Init the camera. preset = board name (ai-thinker, esp-eye, ...).
        framesize: framesize_t enum (7 = VGA). quality: JPEG 0 (best)..63."""
        mgr.bridge().camera.begin(preset, framesize=framesize, quality=quality,
                                  xclk_mhz=xclk_mhz, fb_count=fb_count)
        return f"camera initialized ({preset})"

    @mcp.tool
    @guarded
    def camera_capture():
        """Capture one JPEG frame and return it as an image."""
        jpeg = mgr.bridge().camera.capture()
        try:
            from fastmcp.utilities.types import Image

            return Image(data=jpeg, format="jpeg")
        except (ImportError, AttributeError):
            import base64

            return {"format": "jpeg", "size": len(jpeg),
                    "base64": base64.b64encode(jpeg).decode()}

    @mcp.tool
    @guarded
    def camera_set(prop: str, value: int) -> str:
        """Tune the sensor: framesize, quality, brightness, contrast, saturation,
        vflip, hmirror, ... (see the camera module for the full list)."""
        mgr.bridge().camera.set(prop, value)
        return f"camera {prop} = {value}"

    @mcp.tool
    @guarded
    def camera_end() -> str:
        """Power down the camera."""
        mgr.bridge().camera.end()
        return "camera stopped"


# ---- ota ---------------------------------------------------------------------

def register_ota(mcp, mgr) -> None:
    @mcp.tool
    @guarded
    def ota_flash(file_path: str, reboot: bool = True) -> dict:
        """Flash a compiled firmware image (.bin file on the host) to the board's
        inactive app slot over the link. Returns bytes written."""
        written = mgr.bridge().ota.flash(file_path, reboot=reboot)
        return {"bytes_written": written, "rebooted": reboot}

    @mcp.tool
    @guarded
    def ota_abort() -> str:
        """Abort an in-progress OTA update."""
        mgr.bridge().ota.abort()
        return "OTA aborted"
