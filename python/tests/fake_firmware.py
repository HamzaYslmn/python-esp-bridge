"""A minimal in-process stand-in for real ESP32 firmware, enabling fully hardware-free tests.

Implements the subset of the bridge protocol used by the test suite: SYS commands
(ping, info, set_baud, free_heap), GPIO, PWM, I2C, SPI, Wi-Fi, NET sockets,
ESP-NOW, RMT, 1-Wire, NVS, FS, sleep, OTA, TWAI, I2S, MCPWM, ETH, and camera.

All responses are produced synchronously inside MockTransport.write(), so tests
don't need threads or timing to get replies.
"""
from __future__ import annotations

import struct

from espbridge import constants as C
from espbridge.protocol import FrameSplitter, decode_frame, encode_frame
from espbridge.transports import MockTransport

CAPS = (C.Cap.WIFI | C.Cap.BLE | C.Cap.BLE_FW | C.Cap.DAC | C.Cap.TOUCH | C.Cap.ESPNOW
        | C.Cap.RMT | C.Cap.ONEWIRE | C.Cap.TWAI | C.Cap.I2S | C.Cap.FS | C.Cap.NVS
        | C.Cap.OTA | C.Cap.ETH | C.Cap.CAM | C.Cap.MCPWM | C.Cap.SLEEP)


class FakeFirmware:
    def __init__(self, proto_version: int = C.PROTOCOL_VERSION,
                 mac: str = "24a160123456", name: str = "",
                 password: str | None = None):
        self.transport = MockTransport(responder=self._on_host_bytes)
        self.proto_version = proto_version
        self.mac = mac
        self.name = name
        # When a password is given the fake behaves like a BLE link: it rejects
        # every command except SYS_AUTH with ST_DENIED until authentication succeeds.
        self.password = password
        self.authed = password is None  # USB (no password) is considered pre-authenticated
        if password is not None:        # mark the mock transport as a BLE link
            self.transport.needs_auth = True
            self.transport.has_baud = False
        self._splitter = FrameSplitter()

        self.gpio_modes: dict[int, int] = {}
        self.gpio_levels: dict[int, int] = {}
        self.watching: dict[int, tuple[int, int]] = {}

        self.watches: dict[int, dict] = {}  # id -> watch rule (WATCH_ADD)
        self.watch_supported = True         # set False to simulate pre-0.5.0 firmware
        self.watch_add2_supported = True    # set False to simulate pre-0.16.0 firmware

        self.wifi_connected = False
        self.wifi_ssid: str | None = None

        self.pwm_attached: dict[int, tuple[int, int]] = {}  # pin -> (freq, res)
        self.pwm_duty: dict[int, int] = {}

        self.i2c_inited = False
        self.i2c_devices: dict[int, bytes] = {}  # address -> bytes returned by I2C_READ
        self.i2c_writes: list[tuple[int, bytes]] = []

        self.spi_inited = False
        self.spi_transfers: list[bytes] = []  # record of bytes sent; the fake loopbacks rx = tx

        self.next_handle = 1
        self.tcp_sent: dict[int, bytes] = {}
        self.udp_sent: list[tuple[int, str, int, bytes]] = []
        self.window_acks: list[tuple[int, int]] = []
        self.closed_handles: list[int] = []

        self.espnow_inited = False
        self.espnow_pmk: bytes | None = None
        self.espnow_peers: dict[bytes, bytes | None] = {}  # peer MAC -> LMK (None = unencrypted)
        self.espnow_sent: list[tuple[bytes, bytes]] = []   # list of (dest_mac, payload) tuples
        self.espnow_deliver = True  # controls the delivery-ACK flag returned in ESPNOW_SEND replies
        self.espnow_ps: tuple[int, int] | None = None  # (wake window ms, wake interval ms; 0 = keep)

        self.rmt_pins: dict[int, tuple[int, int]] = {}  # pin -> (direction, tick_hz)
        self.rmt_tx: list[tuple[int, list[tuple[int, int]]]] = []  # (pin, symbol_list) for each RMT_TX call
        self.rmt_tx_bytes: list[tuple[int, int, int, bytes]] = []  # (pin, bit0_word, bit1_word, data) for RMT_TX_BYTES
        self.rmt_loops: dict[int, list[tuple[int, int]]] = {}  # pin -> symbols currently looping
        self.rmt_carrier: dict[int, tuple[int, int, int]] = {}  # pin -> (freq_hz, duty_pct, enabled)
        self.rmt_capture: list[tuple[int, int]] = []  # symbols returned by the next RMT_RECV call
        self.rmt_recv_args: list[tuple] = []  # recorded args per RMT_RECV: (pin, idle, timeout, max_syms, trigger)

        self.ow_devices: list[bytes] = []  # 8-byte ROM codes present on the simulated 1-Wire bus
        self.ow_writes: list[bytes] = []   # data bytes from each OW_WRITE call, in order
        self.ow_read_data = b""            # bytes consumed sequentially by OW_READ replies
        self._ow_search: list[bytes] = []  # devices still in contention during a ROM-search walk
        self._ow_bit = 0                   # current bit position in the ROM-search algorithm

        self.nvs: dict[str, bytes] = {}

        self.fs_mounted: set[int] = set()
        self.fs_files: dict[tuple[int, str], bytearray] = {}  # (fs_id, path) -> file contents
        self._fds: dict[int, tuple[int, str, int]] = {}  # fd -> (fs_id, path, current_position)

        self.sleeps: list[tuple[int, int, int, int]] = []  # (mode, duration_us, wake_pin, wake_level)
        self.wake_cause = 0
        self.cpu_mhz = 240
        self.ble_central = True  # set False to model a USB session (LINK_POWER -> NOT_INIT)
        self.link_power_mode: int | None = None
        self.radio_off = False              # SYS_RADIO_OFF was accepted
        self.radio_off_supported = True     # set False to simulate pre-0.16.0 firmware

        self.ota_size: int | None = None
        self.ota_data = bytearray()
        self.ota_committed: bool | None = None
        self.ota_has_partition = True

        self.twai_config: tuple | None = None  # (tx_pin, rx_pin, mode, baud_preset, accept_filter)
        self.twai_sent: list[tuple[int, int, bytes]] = []  # (flags, message_id, data) per TWAI_SEND

        self.i2s_config: tuple | None = None  # (direction, bclk_pin, ws_pin, dout_pin, din_pin, sample_rate, bits, stereo)
        self.i2s_written = bytearray()
        self.i2s_capture = b""  # served by I2S_READ

        self.mcpwm_config: tuple | None = None  # (pin_a, pin_b, freq_hz, deadtime_ns)
        self.mcpwm_duty: int | None = None  # duty cycle in permille (0–1000)

        self.eth_config: tuple | None = None  # ("rmii" or "spi", *interface-specific params)
        self.cam_config: bytes | None = None   # raw init payload from CAM_INIT
        self.cam_frame = b""  # JPEG bytes served by CAM_CAPTURE + CAM_READ
        self.cam_props: dict[int, int] = {}
        self.cam_released = False

        self.baud_requests: list[int] = []
        self.blackhole_cmds: set[int] = set()  # commands silently ignored forever (simulate a stuck firmware)
        self.drop_once_cmds: set[int] = set()  # swallow the next matching frame once, then clear (simulate packet loss)
        self.handled: list[int] = []           # command codes received by _handle, in order (used to count retries)

    # ---- helpers -------------------------------------------------------------

    def boot(self) -> None:
        """Emit the SYS_READY banner (what real firmware does at end of setup())."""
        self.transport.inject(encode_frame(C.FLAG_EVENT, 0, C.SYS_READY, self._info()))

    def emit(self, cmd: int, payload: bytes = b"") -> None:
        """Inject an async event, as if a firmware task produced it."""
        self.transport.inject(encode_frame(C.FLAG_EVENT, 0, cmd, payload))

    def emit_espnow_rx(self, src_mac: bytes, data: bytes, rssi: int = -50) -> None:
        """Inject an incoming ESP-NOW packet."""
        self.emit(C.ESPNOW_RX_EVT, src_mac + struct.pack(">b", rssi) + data)

    def _info(self) -> bytes:
        # ... | gpio_count u8 | flash_mb u8 | name[] (rest of the payload)
        return (
            bytes([self.proto_version, 0, 3, 0, C.ChipModel.ESP32, 3])
            + bytes.fromhex(self.mac)
            + struct.pack(">I", int(CAPS))
            + bytes([40, 4])
            + self.name.encode()
        )

    def _reply(self, seq: int, cmd: int, payload: bytes = b"") -> None:
        if seq:
            self.transport.inject(encode_frame(0, seq, cmd, payload))

    def _reply_err(self, seq: int, cmd: int, status: int) -> None:
        if seq:
            self.transport.inject(encode_frame(C.FLAG_ERROR, seq, cmd, bytes([status])))

    # ---- host -> firmware ------------------------------------------------------

    def _on_host_bytes(self, data: bytes) -> None:
        for chunk in self._splitter.feed(data):
            frame = decode_frame(chunk)  # corrupt frames from tests indicate a bug in the encoder
            self._handle(frame.seq, frame.cmd, frame.payload)

    def _handle(self, seq: int, cmd: int, p: bytes) -> None:
        self.handled.append(cmd)
        if cmd in self.drop_once_cmds:  # emulate a single lost frame (lossy link simulation)
            self.drop_once_cmds.discard(cmd)
            return
        if cmd in self.blackhole_cmds:
            return

        # ---- wireless auth gate (mirrors protocol.cpp handle_auth) ----
        if cmd == C.SYS_AUTH:
            if self.password is None or p == self.password.encode():
                self.authed = True
                self._reply(seq, cmd)
                if self.password is not None:
                    self.boot()  # READY banner follows successful auth (BLE)
            else:
                self._reply_err(seq, cmd, C.Status.DENIED)
            return
        if not self.authed:
            self._reply_err(seq, cmd, C.Status.DENIED)
            return

        # ---- SYS ----
        if cmd == C.SYS_PING:
            self._reply(seq, cmd, p)
        elif cmd == C.SYS_INFO:
            self._reply(seq, cmd, self._info())
        elif cmd == C.SYS_SET_BAUD:
            self.baud_requests.append(struct.unpack(">I", p)[0])
            self._reply(seq, cmd)
        elif cmd == C.SYS_FREE_HEAP:  # firmware >= 0.3.2 added a 5th u32 for link-layer RX drop count
            self._reply(seq, cmd, struct.pack(">5I", 200_000, 150_000, 100_000, 0, 0))
        elif cmd == C.SYS_SET_NAME:
            if len(p) > C.BRIDGE_NAME_MAX:
                self._reply_err(seq, cmd, C.Status.BAD_ARGS)
            else:
                self.name = p.decode()
                self._reply(seq, cmd)

        # ---- GPIO ----
        elif cmd == C.GPIO_SET_MODE:
            self.gpio_modes[p[0]] = p[1]
            self._reply(seq, cmd)
        elif cmd == C.GPIO_WRITE:
            if p[0] not in self.gpio_modes:
                self._reply_err(seq, cmd, C.Status.BAD_PIN)
            else:
                self.gpio_levels[p[0]] = p[1]
                self._reply(seq, cmd, bytes([p[1]]))  # reply payload is the confirmed level (read-back ACK)
        elif cmd == C.GPIO_READ:
            self._reply(seq, cmd, bytes([self.gpio_levels.get(p[0], 0)]))
        elif cmd == C.GPIO_WRITE_MASK:
            mask, vals = struct.unpack(">QQ", p)
            for pin in range(64):
                if mask >> pin & 1:
                    self.gpio_levels[pin] = vals >> pin & 1
            self._reply(seq, cmd)
        elif cmd == C.GPIO_WATCH:
            pin, edge, db = struct.unpack(">BBH", p)
            self.watching[pin] = (edge, db)
            self._reply(seq, cmd)
        elif cmd == C.GPIO_UNWATCH:
            self.watching.pop(p[0], None)
            self._reply(seq, cmd)
        elif cmd == C.GPIO_STATUS:  # response layout: level u8 | mode u8 | pwm_freq u32 | pwm_duty u32
            pin = p[0]
            level = self.gpio_levels.get(pin, 0)
            mode = self.gpio_modes.get(pin, 0xFF)
            freq = self.pwm_attached.get(pin, (0, 0))[0]
            duty = self.pwm_duty.get(pin, 0)
            self._reply(seq, cmd, bytes([level, mode]) + struct.pack(">II", freq, duty))
        elif cmd == C.GPIO_DUMP:  # response: count u8, then per active pin: pin u8 | mode u8 | level u8 | freq u32 | duty u32
            active = sorted(set(self.gpio_modes) | set(self.pwm_attached))
            out = bytes([len(active)])
            for pin in active:
                mode = self.gpio_modes.get(pin, 0xFF)
                level = self.gpio_levels.get(pin, 0)
                freq = self.pwm_attached.get(pin, (0, 0))[0]
                duty = self.pwm_duty.get(pin, 0)
                out += bytes([pin, mode, level]) + struct.pack(">II", freq, duty)
            self._reply(seq, cmd, out)

        # ---- PWM ----
        elif cmd == C.PWM_ATTACH:
            pin, freq, res = struct.unpack(">BIB", p)
            self.pwm_attached[pin] = (freq, res)
            self._reply(seq, cmd)
        elif cmd == C.PWM_WRITE:
            pin, duty = struct.unpack(">BI", p)
            if pin not in self.pwm_attached:
                self._reply_err(seq, cmd, C.Status.NOT_INIT)
            else:
                self.pwm_duty[pin] = duty
                self._reply(seq, cmd)
        elif cmd == C.PWM_DETACH:
            self.pwm_attached.pop(p[0], None)
            self.pwm_duty.pop(p[0], None)
            self._reply(seq, cmd)

        # ---- I2C ----
        elif cmd == C.I2C_INIT:
            self.i2c_inited = True
            self._reply(seq, cmd, struct.pack(">H", C.MAX_PAYLOAD))
        elif cmd == C.I2C_SCAN:
            addrs = sorted(self.i2c_devices)
            self._reply(seq, cmd, bytes([len(addrs)]) + bytes(addrs))
        elif cmd == C.I2C_WRITE:
            if p[1] not in self.i2c_devices:
                self._reply_err(seq, cmd, C.Status.IO)
            else:
                self.i2c_writes.append((p[1], p[2:]))
                self._reply(seq, cmd)
        elif cmd == C.I2C_READ:
            if p[1] not in self.i2c_devices:
                self._reply_err(seq, cmd, C.Status.IO)
            else:
                self._reply(seq, cmd, self.i2c_devices[p[1]][: p[2]])
        elif cmd == C.I2C_WRITE_READ:
            addr, wlen = p[1], p[2]
            if addr not in self.i2c_devices:
                self._reply_err(seq, cmd, C.Status.IO)
            else:
                self.i2c_writes.append((addr, p[3 : 3 + wlen]))
                rlen = p[3 + wlen]
                self._reply(seq, cmd, self.i2c_devices[addr][:rlen])

        # ---- SPI (loopback: the fake always echoes tx bytes back as rx) ----
        elif cmd == C.SPI_INIT:
            self.spi_inited = True
            self._reply(seq, cmd)
        elif cmd == C.SPI_TRANSFER:
            if not self.spi_inited:
                self._reply_err(seq, cmd, C.Status.NOT_INIT)
            else:
                self.spi_transfers.append(p[2:])
                self._reply(seq, cmd, p[2:])
        elif cmd == C.SPI_DEINIT:
            self.spi_inited = False
            self._reply(seq, cmd)

        # ---- WIFI ----
        elif cmd == C.WIFI_CONNECT:
            slen = p[0]
            self.wifi_ssid = p[1 : 1 + slen].decode()
            self.wifi_connected = True
            self._reply(seq, cmd)
        elif cmd == C.WIFI_STATUS:
            ip = bytes([192, 168, 1, 50]) if self.wifi_connected else bytes(4)
            status = 3 if self.wifi_connected else 6  # WL_CONNECTED / WL_DISCONNECTED
            payload = (bytes([status]) + ip + bytes([192, 168, 1, 1]) + bytes([255, 255, 255, 0])
                       + struct.pack(">bB", -55, 6) + bytes.fromhex("24a160123456"))
            self._reply(seq, cmd, payload)
        elif cmd == C.WIFI_SCAN:
            self._reply(seq, cmd)
            for i, (ssid, rssi) in enumerate([(b"HomeWifi", -40), (b"Neighbor", -70)]):
                res = (bytes([i, 2]) + struct.pack(">b", rssi) + bytes([3, 6])
                       + bytes(6) + bytes([len(ssid)]) + ssid)
                self.emit(C.WIFI_SCAN_RES, res)
            self.emit(C.WIFI_SCAN_DONE, bytes([2]))
        elif cmd == C.WIFI_DISCONNECT:
            self.wifi_connected = False
            self._reply(seq, cmd)
        elif cmd == C.WIFI_AP_STOP:
            self._reply(seq, cmd)

        # ---- NET ----
        elif cmd == C.NET_TCP_CONNECT:
            if not self.wifi_connected:
                self._reply_err(seq, cmd, C.Status.SOCKET)
                return
            h = self.next_handle
            self.next_handle += 1
            self.tcp_sent[h] = b""
            self._reply(seq, cmd, bytes([h]))
        elif cmd == C.NET_TCP_LISTEN or cmd == C.NET_UDP_OPEN:
            h = self.next_handle
            self.next_handle += 1
            self._reply(seq, cmd, bytes([h]))
        elif cmd == C.NET_SEND:
            h = p[0]
            if h not in self.tcp_sent:
                self._reply_err(seq, cmd, C.Status.SOCKET)
            else:
                self.tcp_sent[h] += p[1:]
                self._reply(seq, cmd, struct.pack(">H", len(p) - 1))
        elif cmd == C.NET_SEND_TO:
            ip = ".".join(str(x) for x in p[1:5])
            (port,) = struct.unpack_from(">H", p, 5)
            self.udp_sent.append((p[0], ip, port, p[7:]))
            self._reply(seq, cmd)
        elif cmd == C.NET_CLOSE:
            self.closed_handles.append(p[0])
            self._reply(seq, cmd)
        elif cmd == C.NET_WINDOW_ACK:
            (h, n) = struct.unpack(">BH", p)
            self.window_acks.append((h, n))
            # NET_WINDOW_ACK is fire-and-forget: the firmware never sends a reply

        # ---- ESP-NOW ----
        elif cmd == C.ESPNOW_INIT:
            self.espnow_inited = True
            self._reply(seq, cmd, bytes.fromhex(self.mac))
        elif cmd == C.ESPNOW_DEINIT:
            self.espnow_inited = False
            self.espnow_peers.clear()
            self._reply(seq, cmd)
        elif cmd == C.ESPNOW_SET_PMK:
            if len(p) != 16:
                self._reply_err(seq, cmd, C.Status.BAD_ARGS)
            else:
                self.espnow_pmk = p
                self._reply(seq, cmd)
        elif cmd == C.ESPNOW_ADD_PEER:
            if len(p) not in (8, 24):
                self._reply_err(seq, cmd, C.Status.BAD_ARGS)
            else:
                self.espnow_peers[p[:6]] = p[8:24] if len(p) == 24 else None
                self._reply(seq, cmd)
        elif cmd == C.ESPNOW_POWER_SAVE:
            if len(p) != 4:
                self._reply_err(seq, cmd, C.Status.BAD_ARGS)
            else:
                self.espnow_ps = tuple(struct.unpack(">HH", p))
                self._reply(seq, cmd)
        elif cmd == C.ESPNOW_DEL_PEER:
            self.espnow_peers.pop(p[:6], None)
            self._reply(seq, cmd)
        elif cmd == C.ESPNOW_SEND:
            if not self.espnow_inited:
                self._reply_err(seq, cmd, C.Status.NOT_INIT)
            elif p[:6] not in self.espnow_peers:
                self._reply_err(seq, cmd, C.Status.BAD_ARGS)
            else:
                self.espnow_sent.append((p[:6], p[6:]))
                if seq:  # synchronous send: reply immediately with the delivery status byte
                    self._reply(seq, cmd, bytes([1 if self.espnow_deliver else 0]))
                else:  # fire-and-forget (wait=False): delivery result arrives as an async event
                    self.emit(C.ESPNOW_SEND_EVT,
                              p[:6] + bytes([0 if self.espnow_deliver else 1]))

        # ---- RMT ----
        elif cmd == C.RMT_INIT:
            pin, direction, hz = struct.unpack(">BBI", p)
            self.rmt_pins[pin] = (direction, hz)
            self._reply(seq, cmd)
        elif cmd == C.RMT_DEINIT:
            self.rmt_pins.pop(p[0], None)
            self.rmt_loops.pop(p[0], None)
            self._reply(seq, cmd)
        elif cmd in (C.RMT_TX, C.RMT_TX_LOOP):
            if p[0] not in self.rmt_pins:
                self._reply_err(seq, cmd, C.Status.NOT_INIT)
                return
            syms = [((s >> 15) & 1, s & 0x7FFF)
                    for (s,) in struct.iter_unpack(">H", p[1:])]
            if cmd == C.RMT_TX:
                self.rmt_tx.append((p[0], syms))
            else:
                self.rmt_loops[p[0]] = syms
            self._reply(seq, cmd)
        elif cmd == C.RMT_TX_BYTES:
            if p[0] not in self.rmt_pins:
                self._reply_err(seq, cmd, C.Status.NOT_INIT)
                return
            bit0, bit1 = struct.unpack_from(">II", p, 1)
            self.rmt_tx_bytes.append((p[0], bit0, bit1, p[9:]))
            self._reply(seq, cmd)
        elif cmd == C.RMT_TX_STOP:
            self.rmt_loops.pop(p[0], None)
            self._reply(seq, cmd)
        elif cmd == C.RMT_RECV:
            pin, idle, timeout, max_syms, tpin, tlevel, tus = struct.unpack(">BHHHBBI", p)
            if pin not in self.rmt_pins:
                self._reply_err(seq, cmd, C.Status.NOT_INIT)
                return
            self.rmt_recv_args.append((pin, idle, timeout, max_syms,
                                       None if tpin == 0xFF else (tpin, tlevel, tus)))
            out = b"".join(struct.pack(">H", (lv << 15) | dur)
                           for lv, dur in self.rmt_capture[:max_syms])
            self._reply(seq, cmd, out)
        elif cmd == C.RMT_CARRIER:
            pin, freq, duty, en = struct.unpack(">BIBB", p)
            self.rmt_carrier[pin] = (freq, duty, en)
            self._reply(seq, cmd)

        # ---- ONEWIRE (emulates a multi-device bus incl. ROM search) ----
        elif cmd == C.OW_RESET:
            self._reply(seq, cmd, bytes([1 if self.ow_devices else 0]))
        elif cmd == C.OW_WRITE:
            data = p[2:]
            self.ow_writes.append(data)
            if data[:1] == b"\xf0":  # 0xF0 = SEARCH_ROM: reset the triplet walk state
                self._ow_search = list(self.ow_devices)
                self._ow_bit = 0
            self._reply(seq, cmd)
        elif cmd == C.OW_READ:
            out, self.ow_read_data = self.ow_read_data[: p[1]], self.ow_read_data[p[1]:]
            self._reply(seq, cmd, out.ljust(p[1], b"\xff"))
        elif cmd == C.OW_TRIPLET:
            bit = self._ow_bit
            bits = {dev[bit // 8] >> (bit % 8) & 1 for dev in self._ow_search}
            if not bits:  # no devices on this branch of the search tree
                self._reply(seq, cmd, bytes([1, 1, 1]))
                return
            id_bit = 0 if 0 in bits else 1     # wired-AND across all devices: 0 if any device has a 0
            cmp_bit = 0 if 1 in bits else 1     # complement: 0 if any device has a 1
            taken = id_bit if id_bit != cmp_bit else p[1]  # unambiguous bit, or use the direction the host chose
            self._ow_search = [d for d in self._ow_search
                               if d[bit // 8] >> (bit % 8) & 1 == taken]
            self._ow_bit += 1
            self._reply(seq, cmd, bytes([id_bit, cmp_bit, taken]))

        # ---- SYS sleep ----
        elif cmd == C.SYS_SLEEP:
            mode, us, pin, level = struct.unpack(">BQbB", p)
            self.sleeps.append((mode, us, pin, level))
            if mode == 0:
                self._reply(seq, cmd)  # deep sleep: reply OK immediately; the real board would reboot after this
            else:
                # Light sleep returns a wake cause: 4 = timer expired, 7 = GPIO edge
                self.wake_cause = 4 if us else 7
                self._reply(seq, cmd, bytes([self.wake_cause]))
        elif cmd == C.SYS_WAKE_CAUSE:
            self._reply(seq, cmd, bytes([self.wake_cause]))
        elif cmd == C.SYS_CPU_FREQ:
            if p[0] not in (80, 160, 240):
                self._reply_err(seq, cmd, C.Status.BAD_ARGS)
            else:
                self.cpu_mhz = p[0]
                self._reply(seq, cmd, bytes([p[0]]))
        elif cmd == C.SYS_LINK_POWER:
            # The real firmware replies NOT_INIT when no BLE central is
            # connected; the fake models a BLE session by default.
            if p[0] > 1:
                self._reply_err(seq, cmd, C.Status.BAD_ARGS)
            elif not self.ble_central:
                self._reply_err(seq, cmd, C.Status.NOT_INIT)
            else:
                self.link_power_mode = p[0]
                self._reply(seq, cmd)
        elif cmd == C.SYS_RADIO_OFF:
            if not self.radio_off_supported:
                self._reply_err(seq, cmd, C.Status.UNKNOWN_CMD)
            elif self.ble_central or self.wifi_connected:
                self._reply_err(seq, cmd, C.Status.BUSY)
            else:
                self.radio_off = True
                self._reply(seq, cmd)

        # ---- NVS ----
        elif cmd == C.NVS_SET:
            klen = p[0]
            self.nvs[p[1 : 1 + klen].decode()] = p[1 + klen :]
            self._reply(seq, cmd)
        elif cmd == C.NVS_GET:
            key = p.decode()
            if key in self.nvs:
                self._reply(seq, cmd, self.nvs[key])
            else:
                self._reply_err(seq, cmd, C.Status.NOT_FOUND)
        elif cmd == C.NVS_DEL:
            if self.nvs.pop(p.decode(), None) is None:
                self._reply_err(seq, cmd, C.Status.NOT_FOUND)
            else:
                self._reply(seq, cmd)
        elif cmd == C.NVS_KEYS:
            out = bytes([len(self.nvs)])
            for k in self.nvs:
                out += bytes([len(k)]) + k.encode()
            self._reply(seq, cmd, out)
        elif cmd == C.NVS_CLEAR:
            self.nvs.clear()
            self._reply(seq, cmd)

        # ---- FS (in-memory volumes) ----
        elif cmd == C.FS_MOUNT:
            self.fs_mounted.add(p[0])
            self._reply(seq, cmd, struct.pack(">II", 1024, 16))
        elif cmd == C.FS_UMOUNT:
            self.fs_mounted.discard(p[0])
            self._reply(seq, cmd)
        elif cmd == C.FS_OPEN:
            fsid, mode, path = p[0], p[1], p[2:].decode()
            if mode == 0 and (fsid, path) not in self.fs_files:
                self._reply_err(seq, cmd, C.Status.NOT_FOUND)
                return
            if mode == 1:
                self.fs_files[(fsid, path)] = bytearray()
            buf = self.fs_files.setdefault((fsid, path), bytearray())
            fd = max(self._fds, default=0) + 1
            self._fds[fd] = (fsid, path, len(buf) if mode == 2 else 0)
            self._reply(seq, cmd, struct.pack(">BI", fd, len(buf)))
        elif cmd == C.FS_READ:
            fd, n = struct.unpack(">BH", p)
            fsid, path, pos = self._fds[fd]
            data = bytes(self.fs_files[(fsid, path)][pos : pos + n])
            self._fds[fd] = (fsid, path, pos + len(data))
            self._reply(seq, cmd, data)
        elif cmd == C.FS_WRITE:
            fd = p[0]
            fsid, path, pos = self._fds[fd]
            buf = self.fs_files[(fsid, path)]
            buf[pos : pos + len(p) - 1] = p[1:]
            self._fds[fd] = (fsid, path, pos + len(p) - 1)
            self._reply(seq, cmd, struct.pack(">H", len(p) - 1))
        elif cmd == C.FS_SEEK:
            fd, pos = struct.unpack(">BI", p)
            fsid, path, _ = self._fds[fd]
            self._fds[fd] = (fsid, path, pos)
            self._reply(seq, cmd)
        elif cmd == C.FS_CLOSE:
            self._fds.pop(p[0], None)
            self._reply(seq, cmd)
        elif cmd == C.FS_LIST:
            fsid, prefix = p[0], p[1:].decode().rstrip("/") + "/"
            count = 0
            for (fid, path), data in self.fs_files.items():
                if fid == fsid and path.startswith(prefix) and "/" not in path[len(prefix):]:
                    name = path[len(prefix):]
                    self.emit(C.FS_LIST_EVT,
                              bytes([0]) + struct.pack(">I", len(data)) + name.encode())
                    count += 1
            self._reply(seq, cmd, struct.pack(">H", count))
        elif cmd == C.FS_STAT:
            key = (p[0], p[1:].decode())
            if key not in self.fs_files:
                self._reply_err(seq, cmd, C.Status.NOT_FOUND)
            else:
                self._reply(seq, cmd,
                            struct.pack(">IBI", len(self.fs_files[key]), 0, 1700000000))
        elif cmd == C.FS_REMOVE:
            if self.fs_files.pop((p[0], p[1:].decode()), None) is None:
                self._reply_err(seq, cmd, C.Status.NOT_FOUND)
            else:
                self._reply(seq, cmd)
        elif cmd == C.FS_RENAME:
            flen = p[1]
            src = (p[0], p[2 : 2 + flen].decode())
            dst = (p[0], p[2 + flen :].decode())
            if src not in self.fs_files:
                self._reply_err(seq, cmd, C.Status.NOT_FOUND)
            else:
                self.fs_files[dst] = self.fs_files.pop(src)
                self._reply(seq, cmd)
        elif cmd == C.FS_MKDIR:
            self._reply(seq, cmd)
        elif cmd == C.FS_DF:
            self._reply(seq, cmd, struct.pack(">II", 1024, 16))

        # ---- TWAI ----
        elif cmd == C.TWAI_INIT:
            filt = struct.unpack(">IIB", p[4:13]) if len(p) >= 13 else None
            self.twai_config = (p[0], p[1], p[2], p[3], filt)
            self._reply(seq, cmd)
        elif cmd == C.TWAI_SEND:
            if self.twai_config is None:
                self._reply_err(seq, cmd, C.Status.NOT_INIT)
            else:
                self.twai_sent.append((p[0], struct.unpack(">I", p[1:5])[0], p[5:]))
                self._reply(seq, cmd)
        elif cmd == C.TWAI_STATUS:
            self._reply(seq, cmd, bytes([1, 2, 3]) + struct.pack(">I", 4))
        elif cmd == C.TWAI_RECOVER:
            self._reply(seq, cmd)
        elif cmd == C.TWAI_DEINIT:
            self.twai_config = None
            self._reply(seq, cmd)

        # ---- I2S ----
        elif cmd == C.I2S_INIT:
            self.i2s_config = struct.unpack(">BbbbbIBB", p)
            self._reply(seq, cmd)
        elif cmd == C.I2S_WRITE:
            if self.i2s_config is None:
                self._reply_err(seq, cmd, C.Status.NOT_INIT)
            else:
                self.i2s_written += p
                self._reply(seq, cmd, struct.pack(">H", len(p)))
        elif cmd == C.I2S_READ:
            n = struct.unpack(">H", p)[0]
            out, self.i2s_capture = self.i2s_capture[:n], self.i2s_capture[n:]
            self._reply(seq, cmd, out)
        elif cmd == C.I2S_DEINIT:
            self.i2s_config = None
            self._reply(seq, cmd)

        # ---- MCPWM ----
        elif cmd == C.MCPWM_INIT:
            self.mcpwm_config = struct.unpack(">BbIH", p)
            self._reply(seq, cmd)
        elif cmd == C.MCPWM_DUTY:
            if self.mcpwm_config is None:
                self._reply_err(seq, cmd, C.Status.NOT_INIT)
            else:
                self.mcpwm_duty = struct.unpack(">H", p)[0]
                self._reply(seq, cmd)
        elif cmd == C.MCPWM_STOP:
            self.mcpwm_config = None
            self._reply(seq, cmd)

        # ---- ETH ----
        elif cmd == C.ETH_BEGIN_RMII:
            self.eth_config = ("rmii", *struct.unpack(">BbbbbB", p))
            self._reply(seq, cmd)
            self.emit(C.ETH_STATE_EVT, bytes([2, 10, 0, 0, 9]))  # got IP
        elif cmd == C.ETH_BEGIN_SPI:
            self.eth_config = ("spi", *struct.unpack(">BbbbbbbbB", p))
            self._reply(seq, cmd)
            self.emit(C.ETH_STATE_EVT, bytes([2, 10, 0, 0, 9]))
        elif cmd == C.ETH_STATUS:
            self._reply(seq, cmd, bytes([1, 10, 0, 0, 9, 10, 0, 0, 1])
                        + bytes([255, 255, 255, 0]) + bytes(6))
        elif cmd == C.ETH_STOP:
            self.eth_config = None
            self._reply(seq, cmd)

        # ---- CAM ----
        elif cmd == C.CAM_INIT:
            self.cam_config = p
            self._reply(seq, cmd)
        elif cmd == C.CAM_CAPTURE:
            self.cam_released = False
            self._reply(seq, cmd, struct.pack(">IHHB", len(self.cam_frame),
                                              640, 480, 4))
        elif cmd == C.CAM_READ:
            off, n = struct.unpack(">IH", p)
            self._reply(seq, cmd, self.cam_frame[off : off + n])
        elif cmd == C.CAM_RELEASE:
            self.cam_released = True
            self._reply(seq, cmd)
        elif cmd == C.CAM_SET:
            prop, val = struct.unpack(">Bi", p)
            self.cam_props[prop] = val
            self._reply(seq, cmd)
        elif cmd == C.CAM_DEINIT:
            self.cam_config = None
            self._reply(seq, cmd)

        # ---- WATCH (polled event engine) ----
        elif cmd in (C.WATCH_ADD, C.WATCH_ADD2):
            if not self.watch_supported or (cmd == C.WATCH_ADD2 and not self.watch_add2_supported):
                self._reply_err(seq, cmd, C.Status.UNKNOWN_CMD)
                return
            wid, src, arg, aux, cmpv, flags, period, a, b = struct.unpack_from(">BBBBBBHii", p)
            rule = dict(source=src, arg=arg, aux=aux, cmp=cmpv,
                        flags=flags, period_ms=period, a=a, b=b)
            if cmd == C.WATCH_ADD2:
                rule["enter_action"] = struct.unpack_from(">BBi", p, 16)
                rule["exit_action"] = struct.unpack_from(">BBi", p, 22)
            self.watches[wid] = rule
            self._reply(seq, cmd)
        elif cmd == C.WATCH_REMOVE:
            self.watches.pop(p[0], None)
            self._reply(seq, cmd)
        elif cmd == C.WATCH_CLEAR:
            self.watches.clear()
            self._reply(seq, cmd)
        elif cmd == C.WATCH_LIST:
            out = bytes([len(self.watches)])
            for wid in self.watches:
                out += bytes([wid, 0]) + struct.pack(">i", 0)
            self._reply(seq, cmd, out)

        # ---- OTA ----
        elif cmd == C.OTA_BEGIN:
            if not self.ota_has_partition:
                self._reply_err(seq, cmd, C.Status.UNSUPPORTED)
            else:
                self.ota_size = struct.unpack(">I", p)[0]
                self.ota_data.clear()
                self.ota_committed = None
                self._reply(seq, cmd)
        elif cmd == C.OTA_WRITE:
            if self.ota_size is None:
                self._reply_err(seq, cmd, C.Status.NOT_INIT)
            else:
                self.ota_data += p
                self._reply(seq, cmd, struct.pack(">I", len(self.ota_data)))
        elif cmd == C.OTA_END:
            self.ota_committed = bool(p[0])
            self._reply(seq, cmd)
        elif cmd == C.OTA_ABORT:
            self.ota_size = None
            self._reply(seq, cmd)

        else:
            self._reply_err(seq, cmd, C.Status.UNKNOWN_CMD)
