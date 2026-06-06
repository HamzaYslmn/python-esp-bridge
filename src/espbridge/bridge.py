"""Bridge: connection, reader thread, request/response correlation, events."""
from __future__ import annotations

import dataclasses
import logging
import struct
import threading
import time
from dataclasses import dataclass

from . import constants as C
from ._log import log
from .errors import (
    AuthError,
    BridgeError,
    BridgeTimeoutError,
    NoDeviceError,
    ProtocolError,
    RemoteError,
    UnsupportedError,
)
from .protocol import Frame, FrameSplitter, decode_frame, encode_frame, mac_to_str
from .transports import SerialTransport, find_ports


@dataclass(frozen=True)
class Info:
    protocol: int
    fw_version: tuple[int, int, int]
    chip: C.ChipModel
    chip_rev: int
    mac: str
    caps: C.Cap
    gpio_count: int
    flash_mb: int
    name: str = ""  # user-assigned device name (see Bridge.set_name)

    @classmethod
    def parse(cls, payload: bytes) -> "Info":
        if len(payload) < 18:
            raise ProtocolError(f"short SYS_INFO payload: {len(payload)} bytes")
        proto, maj, mnr, pat, model, rev = struct.unpack_from(">6B", payload)
        mac = mac_to_str(payload[6:12])
        (caps,) = struct.unpack_from(">I", payload, 12)
        gpio_count, flash_mb = payload[16], payload[17]
        name = ""
        if len(payload) > 18:  # optional name tail: len u8 | bytes
            nlen = payload[18]
            name = payload[19 : 19 + nlen].decode("utf-8", "replace")
        try:
            chip = C.ChipModel(model)
        except ValueError:
            chip = C.ChipModel.UNKNOWN
        return cls(proto, (maj, mnr, pat), chip, rev, mac, C.Cap(caps),
                   gpio_count, flash_mb, name)


def _norm_mac(mac: str) -> str:
    return mac.replace(":", "").replace("-", "").lower()


class _Pending:
    __slots__ = ("event", "frame")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.frame: Frame | None = None


class Bridge:
    """Connection to a python-esp-bridge ESP32.

    >>> from espbridge import Bridge
    >>> with Bridge() as esp:         # auto-detects the serial port
    ...     esp.gpio.mode(2, "output")
    ...     esp.gpio.write(2, 1)

    With several boards attached, select one by persistent name or MAC
    (assign names once with ``Bridge(port=...).set_name("relays")``):

    >>> esp = Bridge(name="relays")
    >>> esp = Bridge(mac="24:a1:60:12:34:56")

    Over Bluetooth instead of USB (firmware default password "espbridge",
    change it at the top of firmware/firmware.ino):

    >>> esp = Bridge(ble=True)                      # the only advertising bridge
    >>> esp = Bridge(ble="relays", password="espbridge")
    """

    def __init__(
        self,
        port: str | None = None,
        *,
        name: str | None = None,
        mac: str | None = None,
        ble: bool | str = False,
        password: str | None = None,
        baud: int = 115200,
        upgrade_baud: bool = True,
        target_baud: int | None = None,
        reset_on_open: bool = True,
        timeout: float = 2.0,
        retries: int = 1,
        reset_on_exit: bool = False,
        transport=None,
    ):
        self.timeout = timeout
        # How often request() re-sends after a response timeout. Only commands
        # that are safe to re-execute are retried (see constants.NON_IDEMPOTENT);
        # a lost frame on a busy/lossy link then heals invisibly.
        self.retries = retries
        self.reset_on_exit = False  # set for real only once connected (see below)
        self.info: Info | None = None

        # Candidates: (transport factory, label, usb_chip).
        if transport is not None:
            candidates = [(lambda t=transport: t, "transport",
                           getattr(transport, "usb_chip", None))]
        elif ble:
            candidates = self._ble_candidates(ble, name, mac)
        elif port is not None:
            chip = next((p.usb_chip for p in find_ports() if p.device == port), None)
            candidates = [(lambda p=port, c=chip: SerialTransport(p, baud, usb_chip=c),
                           port, chip)]
        else:
            ports = find_ports()
            if not ports:
                raise NoDeviceError(
                    "no ESP32 serial port found (CP210x/CH340/CH9102/native USB); "
                    "pass port='COM5' / '/dev/ttyUSB0' explicitly — or ble=True"
                )
            # Several ESP32-like ports: probe each in turn and keep the first
            # that answers the bridge handshake (and matches name=/mac= if
            # given) — non-bridge boards just time out and are skipped.
            candidates = [(lambda p=p: SerialTransport(p.device, baud, usb_chip=p.usb_chip),
                           p.device, p.usb_chip) for p in ports]

        probing = len(candidates) > 1
        errors: list[str] = []
        for factory, label, chip in candidates:
            self._reset_state()
            if probing:
                log.debug(f"probing {label} ...")
            try:
                self._t = factory()
            except Exception as e:
                log.debug(f"{label}: open failed: {e}")
                errors.append(f"{label}: {e}")
                continue
            self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                            name="espbridge-reader")
            self._reader.start()
            try:
                if getattr(self._t, "needs_auth", False):
                    self._auth(password)
                    self._handshake(reset_on_open=False)
                else:
                    self._handshake(reset_on_open)
                assert self.info is not None
                if self._matches(name, mac):
                    if upgrade_baud and getattr(self._t, "has_baud", True):
                        self._upgrade_baud(baud, target_baud)
                    self.reset_on_exit = reset_on_exit
                    if probing and name is None and mac is None:
                        others = ", ".join(l for _, l, _ in candidates
                                           if l != label) or "none"
                        log.info(
                            f"auto-selected {label}: "
                            f"name={self.info.name or '(unnamed)'} "
                            f"mac={self.info.mac} "
                            f"chip={self.info.chip.name} "
                            f"(other candidates: {others}; pin one with "
                            f"port=, name=, mac= or ble='name-or-mac')")
                    return
                errors.append(f"{label}: name={self.info.name!r} "
                              f"mac={self.info.mac} (no match)")
                self.close()
            except (BridgeTimeoutError, ProtocolError) as e:
                self.close()
                if not probing:
                    raise
                log.debug(f"{label}: {e}")
                errors.append(f"{label}: {e}")
            except BaseException:
                self.close()
                raise
        raise NoDeviceError("no matching bridge found — " + "; ".join(errors))

    @staticmethod
    def _ble_candidates(ble: bool | str, name: str | None, mac: str | None):
        from .transports.ble import BleTransport, find_ble_devices

        target = ble if isinstance(ble, str) else None
        devs = find_ble_devices()
        if target is not None:
            tmac = _norm_mac(target)
            devs = [d for d in devs
                    if d.name == target or d.device_name == target
                    or _norm_mac(d.address) == tmac or _norm_mac(d.mac) == tmac]
        # The advertised name carries the bridge MAC and custom name: narrow
        # the candidates before connecting when the caller passed name=/mac=.
        # On no match keep the full list (adv data can be stale/truncated) —
        # the post-connect _matches() check stays authoritative either way.
        if mac is not None:
            devs = [d for d in devs if _norm_mac(d.mac) == _norm_mac(mac)] or devs
        if name is not None:
            devs = [d for d in devs if d.device_name == name] or devs
        if not devs:
            what = f" named/at {target!r}" if target else ""
            raise NoDeviceError(
                f"no bridge{what} found over Bluetooth — is the board powered, "
                f"in range, and flashed with BRIDGE_BLE_LINK enabled?"
            )
        # Several bridges advertising: probe in adv order, first auth +
        # handshake wins (the caller prints which one was auto-selected).
        return [(lambda d=d: BleTransport(d.address),
                 f"BLE {d.name or d.address}", None) for d in devs]

    def _auth(self, password: str | None) -> None:
        """Authenticate a wireless link (SYS_AUTH) before the handshake."""
        pw = (C.DEFAULT_PASSWORD if password is None else password).encode()
        try:
            self.request(C.SYS_AUTH, pw, timeout=5.0)
        except RemoteError as e:
            if e.status == C.Status.DENIED:
                raise AuthError(
                    "bridge rejected the password — check BRIDGE_PASSWORD at "
                    "the top of firmware/firmware.ino"
                ) from None
            raise

    def _reset_state(self) -> None:
        self._splitter = FrameSplitter()
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._seq = 0
        self._write_lock = threading.Lock()
        self._unacked = 0  # fire-and-forget bytes since the last reply (see send)
        self._handlers: dict[int | None, list] = {}
        self._handlers_lock = threading.Lock()
        self._ready = threading.Event()
        self._closing = False
        self.info = None
        self.on_event(C.SYS_READY, self._on_ready)
        self.on_event(C.SYS_LOG, self._on_sys_log)

    def _matches(self, name: str | None, mac: str | None) -> bool:
        assert self.info is not None
        if name is not None and self.info.name != name:
            return False
        if mac is not None and _norm_mac(self.info.mac) != _norm_mac(mac):
            return False
        return True

    # ---- lifecycle -----------------------------------------------------------

    def _on_ready(self, payload: bytes) -> None:
        try:
            self.info = Info.parse(payload)
        except ProtocolError:
            return
        self._ready.set()

    def _handshake(self, reset_on_open: bool) -> None:
        # Opening the port usually auto-resets the board (DTR/RTS): wait for the
        # SYS_READY banner, force a reset if it doesn't come, then fall back to
        # polling SYS_INFO (covers boards with auto-reset disabled).
        if self._ready.wait(3.0 if not reset_on_open else 1.5):
            pass
        elif reset_on_open:
            self._t.pulse_reset()
            self._ready.wait(3.0)

        if not self._ready.is_set():
            for _ in range(3):
                try:
                    payload = self.request(C.SYS_INFO, timeout=1.0)
                    self.info = Info.parse(payload)
                    self._ready.set()
                    break
                except BridgeTimeoutError:
                    continue
        if not self._ready.is_set():
            raise BridgeTimeoutError(
                "no response from bridge firmware — is it flashed? (firmware/README.md)"
            )
        assert self.info is not None
        if self.info.protocol != C.PROTOCOL_VERSION:
            raise ProtocolError(
                f"protocol mismatch: firmware speaks v{self.info.protocol}, "
                f"this library v{C.PROTOCOL_VERSION} — reflash firmware.ino or "
                f"update python-esp-bridge"
            )

    def _upgrade_baud(self, current: int, target: int | None) -> None:
        assert self.info is not None
        if C.Cap.NATIVE_USB in self.info.caps:
            return  # USB CDC: baud is meaningless
        if target is None:
            target = C.UPGRADE_BAUD.get(getattr(self._t, "usb_chip", None), 921600)
        if not target or target == current:
            return
        self.request(C.SYS_SET_BAUD, struct.pack(">I", target))
        time.sleep(0.05)  # firmware flushes, then switches
        self._t.set_baudrate(target)
        for _ in range(3):
            try:
                self.ping(b"baud")
                log.debug(f"baud upgraded {current} -> {target}")
                return
            except BridgeTimeoutError:
                continue
        # Could not talk at the new baud: fall back.
        log.warning(f"baud upgrade to {target} failed; falling back to {current}")
        self._t.set_baudrate(current)
        try:
            self.ping(b"fallback")
        except BridgeTimeoutError:
            # The firmware already switched to `target` and can't hear us.
            # Reopen the port: the open-time DTR/RTS toggle resets the board
            # (reliable even where a manual reset pulse is not).
            self._reconnect(current)

    def _reconnect(self, baud: int) -> None:
        """Reopen the serial port and redo the handshake (recovers a dead link)."""
        port = getattr(getattr(self._t, "ser", None), "port", None)
        chip = getattr(self._t, "usb_chip", None)
        if port is None:
            raise BridgeTimeoutError("link lost and transport cannot be reopened")
        log.warning(f"link lost; reopening {port} at {baud} baud")
        self._t.close()
        self._reader.join(timeout=1.0)
        time.sleep(0.3)  # let the device reboot from the close-time reset
        self._reset_state()
        self._t = SerialTransport(port, baud, usb_chip=chip)
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="espbridge-reader")
        self._reader.start()
        self._handshake(reset_on_open=True)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self.reset_on_exit and self._ready.is_set():
            try:
                self.request(C.SYS_RESET, timeout=1.0)
            except Exception:
                pass
        self._t.close()
        if self._reader is not threading.current_thread():
            self._reader.join(timeout=1.0)

    def __enter__(self) -> "Bridge":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- reader thread ----------------------------------------------------------

    def _read_loop(self) -> None:
        while not self._closing:
            try:
                data = self._t.read()
            except Exception as e:
                if not self._closing:
                    log.warning(f"transport read failed ({e}); link is down")
                break  # port closed / unplugged
            if not data:
                continue
            for chunk in self._splitter.feed(data):
                try:
                    frame = decode_frame(chunk)
                except ProtocolError as e:
                    log.debug(f"dropping corrupted frame: {e}")
                    continue  # requester times out & retries
                self._handle_frame(frame)
        # Wake up anyone still waiting.
        with self._pending_lock:
            for p in self._pending.values():
                p.event.set()

    def _handle_frame(self, frame: Frame) -> None:
        if frame.is_event:
            self._dispatch_event(frame)
            return
        with self._pending_lock:
            p = self._pending.get(frame.seq)
        if p is not None:
            p.frame = frame
            p.event.set()

    def _on_sys_log(self, payload: bytes) -> None:
        # Firmware log line (incl. redirected ESP-IDF Wi-Fi/BT logs):
        # level u8 | message. Surface via the espbridge logger.
        if payload:
            msg = payload[1:].decode("utf-8", "replace")
            (log.warning if payload[0] >= 2 else log.info)(f"[fw] {msg}")

    def _dispatch_event(self, frame: Frame) -> None:
        with self._handlers_lock:
            specific = list(self._handlers.get(frame.cmd, ()))
            wildcard = list(self._handlers.get(None, ()))
        for cb in specific:
            try:
                cb(frame.payload)
            except Exception:  # user callbacks must not kill the reader
                log.exception(f"event callback {cb!r} raised")
        for cb in wildcard:
            try:
                cb(frame)
            except Exception:
                log.exception(f"event callback {cb!r} raised")

    # ---- events API ----------------------------------------------------------------

    def on_event(self, cmd: int | None, callback) -> None:
        """Register `callback(payload)` for event `cmd` (None = all events, gets the Frame)."""
        with self._handlers_lock:
            self._handlers.setdefault(cmd, []).append(callback)

    def off_event(self, cmd: int | None, callback) -> None:
        with self._handlers_lock:
            try:
                self._handlers.get(cmd, []).remove(callback)
            except ValueError:
                pass

    # ---- request/response -------------------------------------------------------------

    def _alloc_seq(self) -> int:
        with self._pending_lock:
            for _ in range(255):
                self._seq = self._seq % 255 + 1  # cycles 1..255, 0 is reserved
                if self._seq not in self._pending:
                    self._pending[self._seq] = _Pending()
                    return self._seq
        raise BridgeTimeoutError("255 requests in flight — firmware not answering")

    def request(self, cmd: int, payload: bytes = b"", timeout: float | None = None,
                retries: int | None = None) -> bytes:
        """Send a request and return the response payload (raises RemoteError on error status).

        After a response timeout the request is re-sent up to `retries` times
        (default: Bridge(retries=...)) — but only for commands that are safe
        to execute twice (constants.NON_IDEMPOTENT lists the exceptions).
        The final timeout pings the firmware to tell a lost frame ("link
        alive") apart from a dead link/board in the error message.
        """
        if retries is None:
            retries = 0 if cmd in C.NON_IDEMPOTENT else self.retries
        if not self._ready.is_set():
            retries = 0  # probing/handshake: fail fast, callers retry themselves
        for attempt in range(retries + 1):
            try:
                return self._request_once(cmd, payload, timeout)
            except BridgeTimeoutError:
                if attempt >= retries or self._closing:
                    raise self._timeout_error(cmd, timeout) from None
                log.warning(f"{C.cmd_name(cmd)}: no response, "
                            f"retrying ({attempt + 1}/{retries})")

    def _request_once(self, cmd: int, payload: bytes, timeout: float | None) -> bytes:
        seq = self._alloc_seq()
        p = self._pending[seq]
        debug = log.isEnabledFor(logging.DEBUG)
        if debug:
            log.debug(f"-> {C.cmd_name(cmd)} seq={seq} ({len(payload)} B)")
        try:
            with self._write_lock:
                self._t.write(encode_frame(0, seq, cmd, payload))
            if not p.event.wait(timeout if timeout is not None else self.timeout):
                raise BridgeTimeoutError(f"no response for {C.cmd_name(cmd)}")
            if p.frame is None:
                raise BridgeTimeoutError("connection closed while waiting for response")
            # Any reply proves the firmware consumed every byte sent before it
            # (requests are executed in arrival order): the link RX buffer is
            # empty again, so the fire-and-forget window restarts from zero.
            self._unacked = 0
            if debug:
                log.debug(f"<- {C.cmd_name(cmd)} seq={seq} "
                          f"{'ERR' if p.frame.is_error else 'ok'} "
                          f"({len(p.frame.payload)} B)")
            if p.frame.is_error:
                status = p.frame.payload[0] if p.frame.payload else 0xFF
                raise RemoteError(status, cmd)
            return p.frame.payload
        finally:
            with self._pending_lock:
                self._pending.pop(seq, None)

    def _timeout_error(self, cmd: int, timeout: float | None) -> BridgeTimeoutError:
        """Build a timeout error that says whether the link itself is dead."""
        msg = (f"no response for {C.cmd_name(cmd)} within "
               f"{timeout if timeout is not None else self.timeout:g}s")
        if cmd != C.SYS_PING and self._ready.is_set() and not self._closing:
            try:
                self._request_once(C.SYS_PING, b"alive?", 1.0)
                msg += (" — the link is alive (ping OK), so the request or its "
                        "reply was dropped in transit (radio interference or "
                        "firmware heap pressure — check esp.free_heap()); "
                        "Bridge(retries=...) re-sends safe commands "
                        "automatically")
            except Exception:  # incl. transport write errors on a dead link
                msg += (" — and the bridge no longer answers pings: link lost, "
                        "board reset/brown-out, or firmware stuck (power and "
                        "cable/radio range are the usual suspects)")
        return BridgeTimeoutError(msg)

    def send(self, cmd: int, payload: bytes = b"") -> None:
        """Fire-and-forget (seq=0): the firmware will not reply.

        Unwaited frames get no acknowledgment, so nothing naturally paces
        them — a long pipelined burst (e.g. an OLED frame push) can overrun
        the firmware's link RX buffer, which drops bytes and corrupts frames
        (the classic symptom: a BridgeTimeoutError on the final waited write).
        Once more than the transport's `burst_window` bytes are in flight, a
        ping round-trip drains the pipe before this frame is sent.
        """
        data = encode_frame(0, 0, cmd, payload)
        window = getattr(self._t, "burst_window", None)
        if window and self._ready.is_set() and self._unacked + len(data) > window:
            self.request(C.SYS_PING, b"\x00")  # fence: resets _unacked
        with self._write_lock:
            self._t.write(data)
            self._unacked += len(data)

    # ---- conveniences ---------------------------------------------------------------------

    def ping(self, payload: bytes = b"ping") -> float:
        """Round-trip a payload; returns latency in seconds."""
        t0 = time.perf_counter()
        # retries=0: a retried ping would report 2x latency, and the
        # baud-upgrade/probe paths run their own retry loops around this.
        echoed = self.request(C.SYS_PING, payload, retries=0)
        if echoed != payload:
            raise ProtocolError("ping payload mismatch")
        return time.perf_counter() - t0

    @property
    def caps(self) -> C.Cap:
        assert self.info is not None
        return self.info.caps

    def require(self, cap: C.Cap, what: str) -> None:
        if self.info is not None and cap not in self.info.caps:
            raise UnsupportedError(f"{what} is not available on {self.info.chip.name}")

    def free_heap(self) -> dict:
        v = self.request(C.SYS_FREE_HEAP)
        free, min_free, largest, dropped = struct.unpack_from(">4I", v)
        out = {"free": free, "min_free": min_free, "largest_block": largest,
               "dropped_events": dropped}
        if len(v) >= 20:  # firmware >= 0.3.2: bytes the BLE link RX buffer dropped
            out["link_rx_dropped"] = struct.unpack_from(">I", v, 16)[0]
        return out

    def deep_sleep(self, seconds: float = 0, *, wake_pin: int | None = None,
                   wake_level: int = 1) -> None:
        """Put the ESP32 into deep sleep; it reboots on wake-up.

        The link drops while asleep. Wake on a timer (`seconds`), a GPIO
        level (`wake_pin`/`wake_level`), or both. After a timer wake the
        board boots fresh — reconnect with a new Bridge() or `reset()` flow.

        Not available on classic ESP32 when the BLE link is compiled in
        (IRAM limit) — build with BRIDGE_ENABLE_BLE 0 to enable it there.
        """
        self.require(C.Cap.SLEEP, "sleep")
        self.request(C.SYS_SLEEP, self._sleep_args(0, seconds, wake_pin, wake_level))

    def light_sleep(self, seconds: float = 0, *, wake_pin: int | None = None,
                    wake_level: int = 1) -> int:
        """Pause the ESP32 in light sleep; returns the wake cause (RAM and
        the link survive; the reply arrives after wake-up)."""
        self.require(C.Cap.SLEEP, "sleep")
        r = self.request(C.SYS_SLEEP, self._sleep_args(1, seconds, wake_pin, wake_level),
                         timeout=seconds + self.timeout)
        return r[0]

    def wake_cause(self) -> int:
        """esp_sleep_wakeup_cause_t of the last boot (0 = normal reset,
        2 ext0, 3 ext1, 4 timer, 7 gpio)."""
        return self.request(C.SYS_WAKE_CAUSE)[0]

    @staticmethod
    def _sleep_args(mode: int, seconds: float, wake_pin: int | None,
                    wake_level: int) -> bytes:
        if seconds <= 0 and wake_pin is None:
            raise ValueError("give a timer (seconds) and/or a wake_pin")
        return struct.pack(">BQbB", mode, round(seconds * 1_000_000),
                           -1 if wake_pin is None else wake_pin, wake_level)

    def reset(self) -> None:
        """Soft-reset the ESP32 and wait for it to come back."""
        self._ready.clear()
        self.request(C.SYS_RESET)
        if not self._ready.wait(5.0):
            raise BridgeTimeoutError("bridge did not come back after reset")

    def set_name(self, name: str) -> None:
        """Persist a device name on the ESP32 (NVS) for `Bridge(name=...)` lookup."""
        data = name.encode()
        if len(data) > C.BRIDGE_NAME_MAX:
            raise ValueError(f"name must be at most {C.BRIDGE_NAME_MAX} bytes")
        self.request(C.SYS_SET_NAME, data)
        if self.info is not None:
            self.info = dataclasses.replace(self.info, name=name)

    # ---- sub-APIs (lazy, created on first access) ----------------------------------------------

    _SUBAPIS = {
        "gpio": ("gpio", "Gpio"),
        "adc": ("analog", "Adc"),
        "dac": ("analog", "Dac"),
        "touch": ("analog", "Touch"),
        "pwm": ("pwm", "Pwm"),
        "i2c": ("i2c", "I2c"),
        "spi": ("spi", "Spi"),
        "uart": ("uart", "Uart"),
        "wifi": ("wifi", "Wifi"),
        "net": ("net", "Net"),
        "ble": ("ble", "Ble"),
        "espnow": ("espnow", "EspNow"),
        "rmt": ("rmt", "Rmt"),
        "onewire": ("onewire", "OneWire"),
        "fs": ("fs", "Fs"),
        "nvs": ("nvs", "Nvs"),
        "ota": ("ota", "Ota"),
        "can": ("can", "Can"),
        "i2s": ("i2s", "I2s"),
        "eth": ("eth", "Eth"),
        "camera": ("camera", "Camera"),
        "mcpwm": ("mcpwm", "Mcpwm"),
    }

    def __getattr__(self, name):
        try:
            mod_name, cls_name = self._SUBAPIS[name]
        except KeyError:
            raise AttributeError(name) from None
        import importlib

        obj = getattr(importlib.import_module(f".{mod_name}", __package__), cls_name)(self)
        setattr(self, name, obj)  # cache: next access skips __getattr__
        return obj

    def __dir__(self):
        return [*super().__dir__(), *self._SUBAPIS]


class BridgeSet(list):
    """A list of Bridges with convenience helpers (returned by connect_all)."""

    def by_name(self, name: str) -> "Bridge":
        for b in self:
            if b.info is not None and b.info.name == name:
                return b
        raise NoDeviceError(f"no connected bridge named {name!r}")

    def by_mac(self, mac: str) -> "Bridge":
        for b in self:
            if b.info is not None and _norm_mac(b.info.mac) == _norm_mac(mac):
                return b
        raise NoDeviceError(f"no connected bridge with MAC {mac}")

    def close_all(self) -> None:
        for b in self:
            b.close()

    def __enter__(self) -> "BridgeSet":
        return self

    def __exit__(self, *exc) -> None:
        self.close_all()


def connect_all(**kwargs) -> BridgeSet:
    """Connect to every attached bridge.

    >>> import espbridge
    >>> with espbridge.connect_all() as boards:
    ...     for esp in boards:
    ...         print(esp.info.name or esp.info.mac, esp.info.chip.name)
    ...     boards.by_name("relays").gpio.write(2, 1)
    """
    out = BridgeSet()
    errors: list[str] = []
    for p in find_ports():
        try:
            out.append(Bridge(p.device, **kwargs))
        except BridgeError as e:
            log.warning(f"connect_all: skipping {p.device}: {e}")
            errors.append(f"{p.device}: {e}")
    if not out:
        raise NoDeviceError("no bridges connected"
                            + (" — " + "; ".join(errors) if errors else ""))
    return out
