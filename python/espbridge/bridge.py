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

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imported for editors / type-checkers only — no runtime cost
    from .analog import Adc, Dac, Touch
    from .ble import Ble
    from .camera import Camera
    from .can import Can
    from .espnow import EspNow
    from .eth import Eth
    from .fs import Fs
    from .gpio import Gpio
    from .i2c import I2c
    from .i2s import I2s
    from .mcpwm import Mcpwm
    from .net import Net
    from .nvs import Nvs
    from .onewire import OneWire
    from .ota import Ota
    from .pwm import Pwm
    from .rmt import Rmt
    from .spi import Spi
    from .uart import Uart
    from .watch import Watch
    from .wifi import Wifi


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
        """Decode a SYS_INFO payload into an :class:`Info` (used at handshake)."""
        if len(payload) < 18:
            raise ProtocolError(f"short SYS_INFO payload: {len(payload)} bytes")
        proto, maj, mnr, pat, model, rev = struct.unpack_from(">6B", payload)
        mac = mac_to_str(payload[6:12])
        (caps,) = struct.unpack_from(">I", payload, 12)
        gpio_count, flash_mb = payload[16], payload[17]
        name = ""
        if len(payload) > 18:  # optional trailing name field: len u8 | name bytes
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
    >>> with Bridge() as esp:         # prefer Bluetooth, fall back to USB serial
    ...     esp.gpio.mode(2, "output")
    ...     esp.gpio.write(2, 1)

    Pick the transport with ``ble`` (firmware default password "espbridge",
    change it via EspBridge.begin("...") in the sketch):

    >>> esp = Bridge()                  # ble=True: Bluetooth, else USB serial
    >>> esp = Bridge(ble=False)         # USB / COM port only (Bluetooth off)
    >>> esp = Bridge(ble="relays")      # a named board over Bluetooth only
    >>> esp = Bridge(port="COM7")       # a specific serial port

    With several boards attached, select one by persistent name or MAC
    (assign names once with ``Bridge(port=...).set_name("relays")``):

    >>> esp = Bridge(name="relays")
    >>> esp = Bridge(mac="24:a1:60:12:34:56")

    Thread-safe: share **one** Bridge across threads rather than opening several
    (a serial/BLE link can't be opened twice). Requests from different threads
    are correlated by sequence number and pipeline on the wire, so a slow call
    on one thread doesn't stall fast calls on another. For a process-wide shared
    link with auto-reconnect, use :func:`espbridge.connect` / ``BridgeManager``;
    for ``await``, wrap it with :class:`espbridge.AsyncBridge`.

    Peripherals hang off the bridge as sub-APIs — ``esp.gpio``, ``esp.adc``,
    ``esp.i2c``, ``esp.watch`` and so on (see the table in ``_SUBAPIS``). They
    are created lazily on first access; the annotations just below let editors
    resolve them to their classes and show each method's signature and docstring.
    """

    # Lazily-created sub-APIs (see __getattr__ / _SUBAPIS). Declared at type-check
    # time only — these are NOT real class attributes (no assignment), so
    # __getattr__ still builds them on demand — but they let an editor resolve
    # ``esp.gpio`` -> Gpio, ``esp.watch`` -> Watch, ... and surface each method's
    # signature and docstring instead of showing "Unknown".
    if TYPE_CHECKING:
        gpio: Gpio
        adc: Adc
        dac: Dac
        touch: Touch
        pwm: Pwm
        i2c: I2c
        spi: Spi
        uart: Uart
        wifi: Wifi
        net: Net
        ble: Ble
        espnow: EspNow
        rmt: Rmt
        onewire: OneWire
        fs: Fs
        nvs: Nvs
        ota: Ota
        can: Can
        i2s: I2s
        eth: Eth
        camera: Camera
        mcpwm: Mcpwm
        watch: Watch

    def __init__(
        self,
        port: str | None = None,
        *,
        name: str | None = None,
        mac: str | None = None,
        ble: bool | str | None = True,
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
        # Guards lazy sub-API creation in __getattr__ so two threads that first
        # touch e.g. esp.gpio at once can't both build (and leak) a sub-API.
        self._subapi_lock = threading.Lock()
        # How many times request() re-sends after a response timeout.
        # Only commands that are safe to execute more than once are retried
        # (see constants.NON_IDEMPOTENT for the exceptions); this lets a lost
        # frame on a busy or lossy link heal without the caller noticing.
        self.retries = retries
        self.reset_on_exit = False  # set for real only once connected (see below)
        self.info: Info | None = None

        # Build the ordered list of connection candidates to probe, each a
        # (transport factory, label, usb_chip) tuple. The `ble` flag decides
        # which transports are tried:
        #   ble=True (default) : prefer Bluetooth, then fall back to USB serial
        #   ble=False          : USB serial only — Bluetooth disabled
        #   ble="name"/"mac"   : that Bluetooth device only (no USB fallback)
        # An explicit transport= or port= overrides `ble` entirely.
        if transport is not None:
            candidates = [(lambda t=transport: t, "transport",
                           getattr(transport, "usb_chip", None))]
        elif port is not None:
            chip = next((p.usb_chip for p in find_ports() if p.device == port), None)
            candidates = [(lambda p=port, c=chip: SerialTransport(p, baud, usb_chip=c),
                           port, chip)]
        else:
            candidates = self._auto_candidates(ble, name, mac, baud)

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
            self._start_reader()
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
            except (BridgeTimeoutError, ProtocolError, AuthError) as e:
                # While probing several candidates (e.g. BLE-then-serial in the
                # default path), a timeout/auth-failure on one just moves on to
                # the next; with a single explicit candidate it propagates.
                self.close()
                if not probing:
                    raise
                log.debug(f"{label}: {e}")
                errors.append(f"{label}: {e}")
            except BaseException:
                self.close()
                raise
        raise NoDeviceError("no matching bridge found — " + "; ".join(errors))

    def _auto_candidates(self, ble, name, mac, baud):
        """Candidates for the no-port path: Bluetooth and/or USB serial per `ble`.

        ble=False -> USB serial only (Bluetooth disabled); ble="name"/"mac" ->
        that Bluetooth device only (no USB fallback); ble=True/None (default) ->
        prefer Bluetooth, then fall back to USB serial.
        """
        ble_only = isinstance(ble, str)   # a named target: Bluetooth, no USB fallback
        candidates = []
        if ble is not False:              # True / None / "name": Bluetooth wanted
            try:
                candidates += self._ble_candidates(ble, name, mac)
            except NoDeviceError:
                if ble_only:
                    raise                 # the named board must be present
            except ImportError:
                if ble_only:
                    raise
                log.debug("Bluetooth unavailable (pip install "
                          "'python-esp-bridge[ble]'); using USB serial")
        if not ble_only:                  # USB serial too, unless pinned to a BLE name.
            # First port that handshakes (and matches name=/mac= if given) wins;
            # non-bridge boards simply time out and are skipped.
            candidates += [(lambda p=p: SerialTransport(p.device, baud, usb_chip=p.usb_chip),
                            p.device, p.usb_chip) for p in find_ports()]
        if not candidates:
            raise NoDeviceError(
                "no bridge found — power the board (and flash it: "
                "docs/FIRMWARE.md), then pass port='COM5'/'/dev/ttyUSB0', "
                "ble=True, or ble='name'")
        return candidates

    @staticmethod
    def _ble_candidates(ble: bool | str | None, name: str | None, mac: str | None):
        from .transports.ble import (
            BleTransport, find_ble_devices, find_ble_devices_fast)

        target = ble if isinstance(ble, str) else None

        def _is_target(d) -> bool:
            tmac = _norm_mac(target)
            return (d.name == target or d.device_name == target
                    or _norm_mac(d.address) == tmac or _norm_mac(d.mac) == tmac)

        def _strict_match(d) -> bool:
            if target is not None and not _is_target(d):
                return False
            if mac is not None and _norm_mac(d.mac) != _norm_mac(mac):
                return False
            if name is not None and d.device_name != name:
                return False
            return True

        # Fast path: stop scanning as soon as the first matching bridge appears,
        # rather than waiting for the full discover() window to expire —
        # the common single-board case then connects in well under a second.
        devs = find_ble_devices_fast(_strict_match)
        if not devs:
            # Nothing matched the fast filter. Fall back to a full scan and
            # apply the looser name/mac filtering below. Advertised names can
            # be stale or truncated by the OS; the post-connect _matches()
            # check is the authoritative comparison.
            devs = find_ble_devices()
            if target is not None:
                devs = [d for d in devs if _is_target(d)]
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
        # If several bridges are advertising, probe them in advertisement order;
        # the first one that passes auth + handshake wins. The caller logs
        # which device was auto-selected.
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
                    "bridge rejected the password — check the password passed "
                    "to EspBridge.begin() in the firmware"
                ) from None
            raise

    def _reset_state(self) -> None:
        self._splitter = FrameSplitter()
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._seq = 0
        self._write_lock = threading.Lock()
        # Bytes in flight: written to the transport but not yet known consumed by
        # the firmware. _flow gates concurrent writes against the transport's
        # window so the firmware's link RX buffer can't overflow (the BLE
        # rx-overflow / frame-loss path). Two buckets, because they clear
        # differently:
        #   _unacked    — pending *requests*. Each reserves its bytes before the
        #                 write and releases exactly those bytes when its own
        #                 reply lands, so N concurrent requests stay correctly
        #                 accounted (resetting to zero on any reply would erase
        #                 the others still on the wire).
        #   _send_bytes — fire-and-forget send() frames, which get no reply of
        #                 their own. A request reply proves everything written
        #                 before it (in wire order) was consumed, so any reply
        #                 clears this bucket.
        self._unacked = 0
        self._send_bytes = 0
        self._flow = threading.Condition()
        self._handlers: dict[int | None, list] = {}
        self._handlers_lock = threading.Lock()
        self._ready = threading.Event()
        self._closing = False
        # Set once a request times out AND a confirming ping also fails: the
        # board stopped answering (reset/brown-out/unplug). BridgeManager treats
        # the link as stale and reconnects on the next access. See is_alive().
        self._link_dead = False
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
        # Opening the serial port usually auto-resets the board via DTR/RTS.
        # Wait for the SYS_READY banner; if it doesn't arrive, pulse a manual
        # reset, then fall back to polling SYS_INFO (handles boards where
        # the auto-reset circuit is disabled or absent).
        if not self._ready.wait(3.0 if not reset_on_open else 1.5) and reset_on_open:
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
                "no response from bridge firmware — is it flashed? (docs/FIRMWARE.md)"
            )
        assert self.info is not None
        if self.info.protocol != C.PROTOCOL_VERSION:
            raise ProtocolError(
                f"protocol mismatch: firmware speaks v{self.info.protocol}, "
                f"this library v{C.PROTOCOL_VERSION} — reflash the firmware or "
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
        # Ladder down to the universally-safe 921600 if the target fails, so a
        # too-optimistic target costs one recovery cycle, not a 115200 link.
        for baud in dict.fromkeys((target, 921600)):
            if current < baud <= target and self._try_baud(current, baud):
                return

    def _try_baud(self, current: int, target: int) -> bool:
        """One baud-upgrade attempt; restores a working link at `current` on
        failure (reopening the port to reset the board if it already switched)."""
        # Probe the host driver first: an unsupported rate (e.g. 3M on CP210x)
        # raises here, before the firmware switches — a bad target stays a no-op.
        try:
            self._t.set_baudrate(target)
            self._t.set_baudrate(current)
        except Exception as e:
            log.warning(f"host driver rejected {target} baud ({e})")
            return False
        self.request(C.SYS_SET_BAUD, struct.pack(">I", target))
        time.sleep(0.05)  # let the firmware flush its TX buffer before it switches baud
        self._t.set_baudrate(target)
        for _ in range(3):
            try:
                # A baud-check ping round-trips in ms; short timeout keeps a
                # failed attempt cheap.
                self.request(C.SYS_PING, b"baud", timeout=0.3, retries=0)
                log.debug(f"baud upgraded {current} -> {target}")
                return True
            except BridgeTimeoutError:
                continue
        log.warning(f"baud upgrade to {target} failed; falling back to {current}")
        self._t.set_baudrate(current)
        try:
            self.request(C.SYS_PING, b"fallback", timeout=0.5, retries=0)
        except BridgeTimeoutError:
            # Firmware already switched and can't hear us at `current`. Reopen
            # the port so the open-time DTR/RTS toggle resets the board.
            self._reconnect(current)
        return False

    def _start_reader(self) -> None:
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="espbridge-reader")
        self._reader.start()

    def _reconnect(self, baud: int) -> None:
        """Reopen the serial port and redo the handshake (recovers a dead link)."""
        port = getattr(getattr(self._t, "ser", None), "port", None)
        chip = getattr(self._t, "usb_chip", None)
        if port is None:
            raise BridgeTimeoutError("link lost and transport cannot be reopened")
        log.warning(f"link lost; reopening {port} at {baud} baud")
        self._t.close()
        self._reader.join(timeout=1.0)
        time.sleep(0.3)  # wait for the device to reboot after the close-time reset
        self._reset_state()
        self._t = SerialTransport(port, baud, usb_chip=chip)
        self._start_reader()
        self._handshake(reset_on_open=True)

    def __repr__(self) -> str:
        info = getattr(self, "info", None)
        if info is None:
            return f"<{type(self).__name__} connecting>"
        label = f" {info.name!r}" if info.name else ""
        return (f"<{type(self).__name__} {info.chip.name}{label} "
                f"fw{'.'.join(map(str, info.fw_version))}>")

    def close(self) -> None:
        """Close the link and stop the reader thread (idempotent). If the bridge
        was created with ``reset_on_exit=True``, the board is reset first. Prefer
        a ``with Bridge() as esp:`` block, which calls this automatically."""
        if self._closing:
            return
        self._closing = True
        self._notify_writers()  # unblock any writer waiting on the in-flight window
        if self.reset_on_exit and self._ready.is_set():
            try:
                self.request(C.SYS_RESET, timeout=1.0)
            except Exception:
                pass
        self._t.close()
        if self._reader is not threading.current_thread():
            self._reader.join(timeout=1.0)

    def is_closing(self) -> bool:
        """True once close() has been called (the link is down or going down)."""
        return self._closing

    def is_alive(self) -> bool:
        """False once the link is closing, or the board has stopped answering (a
        request timed out and a follow-up ping confirmed the link is gone — a
        reset, brown-out, or unplug). BridgeManager / espbridge.connect() use
        this to transparently reconnect."""
        return not self._closing and not self._link_dead

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
                    continue  # corrupted frame: the requester will time out and retry
                self._handle_frame(frame)
        # Wake any pending requests so they can surface a "connection closed" error.
        with self._pending_lock:
            for p in self._pending.values():
                p.event.set()
        self._notify_writers()  # unblock any writers waiting on the in-flight window

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
        # SYS_LOG payload: level u8 | message bytes (includes redirected
        # ESP-IDF Wi-Fi/BT log output). Forward through the espbridge logger.
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
            except Exception:  # never let a user callback kill the reader thread
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
        """Unregister an event callback previously added with :meth:`on_event`
        (no-op if it isn't registered)."""
        with self._handlers_lock:
            try:
                self._handlers.get(cmd, []).remove(callback)
            except ValueError:
                pass

    # ---- request/response -------------------------------------------------------------

    def _alloc_seq(self) -> int:
        with self._pending_lock:
            for _ in range(255):
                self._seq = self._seq % 255 + 1  # wraps through 1..255; seq=0 means fire-and-forget
                if self._seq not in self._pending:
                    self._pending[self._seq] = _Pending()
                    return self._seq
        raise BridgeTimeoutError("255 requests in flight — firmware not answering")

    def request(self, cmd: int, payload: bytes = b"", timeout: float | None = None,
                retries: int | None = None) -> bytes:
        """Send a request and return the response payload (raises RemoteError on error status).

        After a response timeout the request is re-sent up to `retries` times
        (default: Bridge(retries=...)), but only for commands that are safe to
        execute more than once (constants.NON_IDEMPOTENT lists the exceptions).
        If all retries are exhausted, a final ping distinguishes a lost frame
        ("link alive, packet dropped") from a dead link or unresponsive board,
        and the distinction is included in the error message.
        """
        if retries is None:
            retries = 0 if cmd in C.NON_IDEMPOTENT else self.retries
        if not self._ready.is_set():
            retries = 0  # during probing / handshake: fail fast; the caller manages its own retries
        for attempt in range(retries + 1):
            try:
                return self._request_once(cmd, payload, timeout)
            except BridgeTimeoutError:
                if attempt >= retries or self._closing:
                    raise self._timeout_error(cmd, timeout) from None
                log.warning(f"{C.cmd_name(cmd)}: no response, "
                            f"retrying ({attempt + 1}/{retries})")
        raise self._timeout_error(cmd, timeout)  # unreachable: loop always returns/raises

    def _reserve_window(self, size: int) -> None:
        """Wait for room in the firmware's RX window, then reserve `size` bytes.

        The wait and the reservation happen under the same lock so that
        concurrent callers can't all pass the capacity check before any of
        them has incremented the counter (which would let them all write at
        once and overflow the buffer).

        The cap is the transport's `max_inflight` — sized to the firmware's
        link RX buffer minus one max frame, so a pipelined burst can't
        overflow it while a slow handler blocks the RX pump. After a wait
        timeout the method falls through rather than deadlocking — the
        firmware buffer can absorb one extra frame."""
        with self._flow:
            window = getattr(self._t, "max_inflight", None)
            if window and self._ready.is_set():
                while self._unacked + self._send_bytes + size > window \
                        and not self._closing:
                    if not self._flow.wait(timeout=self.timeout):
                        break
            self._unacked += size

    def _decr_window(self, size: int, *, clear_send: bool = False) -> None:
        with self._flow:
            self._unacked = max(0, self._unacked - size)
            if clear_send:
                self._send_bytes = 0
            self._flow.notify_all()

    def _ack_window(self, size: int) -> None:
        """A request's reply arrived: release that request's own reserved bytes,
        and clear pending send() bytes (the reply proves they were consumed)."""
        self._decr_window(size, clear_send=True)

    def _release_window(self, size: int) -> None:
        self._decr_window(size)

    def _notify_writers(self) -> None:
        """Wake every writer blocked on the in-flight window (shutdown paths)."""
        with self._flow:
            self._flow.notify_all()

    def _request_once(self, cmd: int, payload: bytes, timeout: float | None) -> bytes:
        seq = self._alloc_seq()
        p = self._pending[seq]
        debug = log.isEnabledFor(logging.DEBUG)
        if debug:
            log.debug(f"-> {C.cmd_name(cmd)} seq={seq} ({len(payload)} B)")
        data = encode_frame(0, seq, cmd, payload)
        self._reserve_window(len(data))
        replied = False
        try:
            with self._write_lock:
                self._t.write(data)
            if not p.event.wait(timeout if timeout is not None else self.timeout):
                raise BridgeTimeoutError(f"no response for {C.cmd_name(cmd)}")
            if p.frame is None:
                raise BridgeTimeoutError("connection closed while waiting for response")
            replied = True
            # Reply landed: release this request's reserved bytes (only its own,
            # so other threads' in-flight requests stay accounted) and clear any
            # fire-and-forget send() bytes written before it.
            self._ack_window(len(data))
            if debug:
                log.debug(f"<- {C.cmd_name(cmd)} seq={seq} "
                          f"{'ERR' if p.frame.is_error else 'ok'} "
                          f"({len(p.frame.payload)} B)")
            if p.frame.is_error:
                status = p.frame.payload[0] if p.frame.payload else 0xFF
                raise RemoteError(status, cmd)
            return p.frame.payload
        finally:
            if not replied:  # write failed or timed out — release the reserved window bytes
                self._release_window(len(data))
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
                # Confirming ping failed too: the board is gone, not just a
                # dropped frame. Flag the link dead so BridgeManager reconnects.
                self._link_dead = True
                msg += (" — and the bridge no longer answers pings: link lost, "
                        "board reset/brown-out, or firmware stuck (power and "
                        "cable/radio range are the usual suspects)")
        return BridgeTimeoutError(msg)

    def send(self, cmd: int, payload: bytes = b"") -> None:
        """Fire-and-forget (seq=0): the firmware does not send a reply.

        Because there is no reply, nothing naturally paces these frames — a
        long pipelined burst (e.g. pushing an OLED framebuffer) can overrun
        the firmware's link RX buffer, which drops bytes and corrupts frames
        (the classic symptom: a BridgeTimeoutError on the final waited write).
        Once more than the transport's `burst_window` bytes are in flight,
        a ping round-trip is used to drain the pipe before this frame is sent.
        """
        data = encode_frame(0, 0, cmd, payload)
        window = getattr(self._t, "burst_window", None)
        if window and self._ready.is_set():
            with self._flow:
                over = self._unacked + self._send_bytes + len(data) > window
            if over:
                self.request(C.SYS_PING, b"\x00")  # fence: a reply clears _send_bytes
        with self._write_lock:
            self._t.write(data)
        with self._flow:
            self._send_bytes += len(data)

    # ---- conveniences ---------------------------------------------------------------------

    def ping(self, payload: bytes = b"ping") -> float:
        """Round-trip a payload; returns latency in seconds."""
        t0 = time.perf_counter()
        # retries=0: retrying a ping would double the reported latency, and
        # the baud-upgrade and probe paths manage their own retry loops.
        echoed = self.request(C.SYS_PING, payload, retries=0)
        if echoed != payload:
            raise ProtocolError("ping payload mismatch")
        return time.perf_counter() - t0

    @property
    def caps(self) -> C.Cap:
        """The board's capability flags (a :class:`constants.Cap` bitset), e.g.
        ``C.Cap.DAC in esp.caps``."""
        assert self.info is not None
        return self.info.caps

    def require(self, cap: C.Cap, what: str) -> None:
        """Raise :class:`UnsupportedError` if the board lacks ``cap``; ``what`` is
        the human name used in the message. Used by sub-APIs to fail clearly on
        chips that don't have a peripheral."""
        if self.info is not None and cap not in self.info.caps:
            raise UnsupportedError(f"{what} is not available on {self.info.chip.name}")

    def free_heap(self) -> dict:
        """Heap stats from the board: ``{"free", "min_free", "largest_block",
        "dropped_events"}`` in bytes, plus ``"link_rx_dropped"`` (fw >= 0.3.2,
        BLE RX overflow bytes) and ``"serial_rx_errors"`` (fw >= 0.5.2,
        corrupted frames received on the USB link). Handy for spotting memory
        pressure, a leak, or link-level loss over time."""
        v = self.request(C.SYS_FREE_HEAP)
        free, min_free, largest, dropped = struct.unpack_from(">4I", v)
        out = {"free": free, "min_free": min_free, "largest_block": largest,
               "dropped_events": dropped}
        if len(v) >= 20:  # fw >= 0.3.2: bytes dropped by the BLE link RX buffer
            out["link_rx_dropped"] = struct.unpack_from(">I", v, 16)[0]
        if len(v) >= 24:  # fw >= 0.5.2: UART RX error events (overflow/framing)
            out["serial_rx_errors"] = struct.unpack_from(">I", v, 20)[0]
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
        2 ext0, 3 ext1, 4 timer, 7 gpio). Available on every build — reading
        the cause is cheap, even where entering sleep isn't supported."""
        return self.request(C.SYS_WAKE_CAUSE)[0]

    def cpu_freq(self, mhz: int) -> int:
        """Set the CPU clock to 80, 160 or 240 MHz; returns the new frequency.

        80 MHz is the floor while any radio (Wi-Fi/BLE) is active and roughly
        halves CPU power vs 240. UART and peripheral clocks ride the APB bus,
        which stays at 80 MHz — baud rates and timings are unaffected."""
        if mhz not in (80, 160, 240):
            raise ValueError(f"mhz must be 80, 160 or 240, got {mhz}")
        return self.request(C.SYS_CPU_FREQ, bytes([mhz]))[0]

    def link_power(self, mode: str = "battery") -> None:
        """BLE link radio profile (needs a connected BLE central).

        "performance" (the default after every connect) tunes the connection
        interval down to the central's floor (7.5-15 ms) for lowest latency.
        "battery" asks for a 100 ms interval with slave latency 4: the idle
        radio wakes ~2x/s instead of ~70-130x/s, and command RTT rises to
        ~0.3-1 s. Centrals apply relaxed parameters quickly but may take
        5-10 s to re-grant fast ones. Per-connection — reconnects start back
        in performance."""
        modes = {"performance": 0, "battery": 1}
        if mode not in modes:
            raise ValueError(f"mode must be one of {sorted(modes)}, got {mode!r}")
        self.request(C.SYS_LINK_POWER, bytes([modes[mode]]))

    def power_mode(self, mode: str) -> dict:
        """One-call power profile: "battery" = 80 MHz CPU + relaxed BLE link;
        "performance" = 240 MHz + fast BLE link. Returns what was applied.

        The BLE part is skipped silently on USB sessions. ESP-NOW receive duty
        is its own (third) knob — see :meth:`espnow.EspNow.power_save`."""
        if mode not in ("performance", "battery"):
            raise ValueError(f"mode must be 'performance' or 'battery', got {mode!r}")
        applied = {"cpu_mhz": self.cpu_freq(80 if mode == "battery" else 240)}
        try:
            self.link_power(mode)
            applied["ble_link"] = mode
        except RemoteError:  # not a BLE session (no central connected)
            applied["ble_link"] = None
        return applied

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
        "watch": ("watch", "Watch"),
    }

    def __getattr__(self, name):
        sub = self._SUBAPIS.get(name)
        if sub is not None:
            import importlib

            with self._subapi_lock:
                # Double-check under the lock: another thread may have created
                # and cached it while we were blocked, in which case reuse that
                # one (building a second would leak its event registrations).
                cached = self.__dict__.get(name)
                if cached is not None:
                    return cached
                mod_name, cls_name = sub
                obj = getattr(importlib.import_module(f".{mod_name}", __package__),
                              cls_name)(self)
                setattr(self, name, obj)  # cache: next access skips __getattr__
                return obj
        # Registered device drivers (bundled in espbridge.drivers, or
        # pip-installed plugins): esp.<name>(...) builds it with this bridge bound to
        # its first argument — esp.dht(4) is DHT(esp, 4). See espbridge.drivers.
        from . import drivers

        cls = drivers.get_driver(name)
        if cls is not None:
            return drivers.BoundDriver(self, cls, name)
        raise AttributeError(name)

    def __dir__(self):
        from . import drivers

        return [*super().__dir__(), *self._SUBAPIS, *drivers.driver_names()]


class BridgeSet(list):
    """A list of Bridges with convenience helpers (returned by connect_all)."""

    def by_name(self, name: str) -> "Bridge":
        """Return the bridge whose persistent name matches; raise
        :class:`NoDeviceError` if none is connected."""
        for b in self:
            if b.info is not None and b.info.name == name:
                return b
        raise NoDeviceError(f"no connected bridge named {name!r}")

    def by_mac(self, mac: str) -> "Bridge":
        """Return the bridge with this MAC (separators/case ignored); raise
        :class:`NoDeviceError` if none is connected."""
        for b in self:
            if b.info is not None and _norm_mac(b.info.mac) == _norm_mac(mac):
                return b
        raise NoDeviceError(f"no connected bridge with MAC {mac}")

    def close_all(self) -> None:
        """Close every bridge in the set (also done by the ``with`` block exit)."""
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
