"""A minimal in-process stand-in for the ESP32 firmware, for hardware-free tests.

Implements just enough of the protocol: SYS (ping/info/set_baud/free_heap),
GPIO state, and the WIFI/NET behaviors the tests exercise. Responses are
produced synchronously inside MockTransport.write().
"""
from __future__ import annotations

import struct

from espbridge import constants as C
from espbridge.protocol import FrameSplitter, decode_frame, encode_frame
from espbridge.transport import MockTransport

CAPS = C.Cap.WIFI | C.Cap.BLE | C.Cap.BLE_FW | C.Cap.DAC | C.Cap.TOUCH


class FakeFirmware:
    def __init__(self, proto_version: int = C.PROTOCOL_VERSION,
                 name: str = "", mac: str = "24a160123456",
                 password: str | None = None):
        self.transport = MockTransport(responder=self._on_host_bytes)
        self.proto_version = proto_version
        self.name = name
        self.mac = mac
        # password set = behave like the BLE link: reject everything except
        # SYS_AUTH with ST_DENIED until the right password arrives.
        self.password = password
        self.authed = password is None
        if password is not None:  # make the mock look like a BLE transport
            self.transport.needs_auth = True
            self.transport.has_baud = False
        self._splitter = FrameSplitter()

        self.gpio_modes: dict[int, int] = {}
        self.gpio_levels: dict[int, int] = {}
        self.watching: dict[int, tuple[int, int]] = {}

        self.wifi_connected = False
        self.wifi_ssid: str | None = None

        self.pwm_attached: dict[int, tuple[int, int]] = {}  # pin -> (freq, res)
        self.pwm_duty: dict[int, int] = {}

        self.i2c_inited = False
        self.i2c_devices: dict[int, bytes] = {}  # addr -> data served on reads
        self.i2c_writes: list[tuple[int, bytes]] = []

        self.spi_inited = False
        self.spi_transfers: list[bytes] = []  # SPI_TRANSFER echoes tx back as rx

        self.next_handle = 1
        self.tcp_sent: dict[int, bytes] = {}
        self.udp_sent: list[tuple[int, str, int, bytes]] = []
        self.window_acks: list[tuple[int, int]] = []
        self.closed_handles: list[int] = []

        self.baud_requests: list[int] = []
        self.blackhole_cmds: set[int] = set()  # commands we never answer

    # ---- helpers -------------------------------------------------------------

    def boot(self) -> None:
        """Emit the SYS_READY banner (what real firmware does at end of setup())."""
        self.transport.inject(encode_frame(C.FLAG_EVENT, 0, C.SYS_READY, self._info()))

    def emit(self, cmd: int, payload: bytes = b"") -> None:
        """Inject an async event, as if a firmware task produced it."""
        self.transport.inject(encode_frame(C.FLAG_EVENT, 0, cmd, payload))

    def _info(self) -> bytes:
        nbytes = self.name.encode()
        return (
            bytes([self.proto_version, 0, 0, 2, C.ChipModel.ESP32, 3])
            + bytes.fromhex(self.mac)
            + struct.pack(">I", int(CAPS))
            + bytes([40, 4])
            + bytes([len(nbytes)]) + nbytes
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
            frame = decode_frame(chunk)  # tests should never send corrupt frames
            self._handle(frame.seq, frame.cmd, frame.payload)

    def _handle(self, seq: int, cmd: int, p: bytes) -> None:
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
        elif cmd == C.SYS_FREE_HEAP:
            self._reply(seq, cmd, struct.pack(">4I", 200_000, 150_000, 100_000, 0))
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
                self._reply(seq, cmd)
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
            self._reply(seq, cmd)
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

        # ---- SPI (loopback: rx echoes tx) ----
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
            # fire-and-forget: no reply

        else:
            self._reply_err(seq, cmd, C.Status.UNKNOWN_CMD)
