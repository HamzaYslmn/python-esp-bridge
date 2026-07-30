"""Bridge: connection, reader thread, request/response correlation, events."""
from __future__ import annotations

import contextlib
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
    needs_fw,
)
from .protocol import Frame, FrameSplitter, decode_frame, encode_frame, mac_to_str
from .transports import SerialTransport, find_ports
from .transports.tcp import TcpListener, TcpTransport, find_wifi_devices

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
    name: str = ""   # stored device name (see Bridge.set_name), "" when unset

    @property
    def ident(self) -> str:
        """What you would pass to ``Bridge()`` to get this board back: its name,
        or its MAC when it hasn't been named. Print this rather than the MAC —
        it's the string that actually selects the board."""
        return self.name or self.mac

    @classmethod
    def parse(cls, payload: bytes) -> Info:
        """Decode a SYS_INFO payload into an :class:`Info` (used at handshake).

        The name is simply the rest of the payload — the frame already carries
        the length, so it needs no length byte of its own. Firmware that sends
        no name yields "".
        """
        if len(payload) < 18:
            raise ProtocolError(f"short SYS_INFO payload: {len(payload)} bytes")
        proto, maj, mnr, pat, model, rev = struct.unpack_from(">6B", payload)
        mac = mac_to_str(payload[6:12])
        (caps,) = struct.unpack_from(">I", payload, 12)
        try:
            chip = C.ChipModel(model)
        except ValueError:
            chip = C.ChipModel.UNKNOWN
        return cls(proto, (maj, mnr, pat), chip, rev, mac, C.Cap(caps),
                   payload[16], payload[17],
                   payload[18:].decode("utf-8", "replace"))


def _norm_mac(mac: str) -> str:
    """A MAC as bare lowercase hex, however it was punctuated."""
    return mac.replace(":", "").replace("-", "").lower()


def _is(target: str, name: str, mac: str) -> bool:
    """Does this board answer to ``target`` — its stored name, or its MAC?

    The whole of identity matching. Used twice: on an advertisement or discovery
    reply before connecting, and on SYS_INFO after — the authoritative one, since
    a broadcast is untrusted input. Whichever halves a link publishes arrive in
    full, so there is no prefix matching and no special case per link.
    """
    return target == name or _norm_mac(target) == _norm_mac(mac)


def _selector(name, mac):
    """The identity selector, however it was spelled.

    ``Bridge("relays")``, ``Bridge(name="relays")`` and ``Bridge(mac="c0:49:…")``
    all mean the same thing — one board — and both keywords accept either form.
    ``mac=`` is there because it reads better when you *are* passing a MAC.
    """
    if name is not None and mac is not None:
        raise TypeError("give a board's identity once: name= or mac=, not both")
    return name if name is not None else mac


def _targets(name) -> tuple[str, ...] | None:
    """The selector as a tuple of targets: one board, or a list of them.

    Both arities share this, so every filter downstream is the same loop.
    """
    if name is None:
        return None
    out = (name,) if isinstance(name, str) else tuple(name)
    if not all(out):
        # "" is the unnamed board's name, so it would match every unnamed board
        # — exactly the silent wrong-board pick the selector exists to prevent.
        raise ValueError("a board selector cannot be empty; pass a name or a "
                         "MAC, or nothing at all to get every board")
    return out


class _Pending:
    __slots__ = ("event", "frame")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.frame: Frame | None = None


# Which Bridge() keywords decide *which boards exist*, as opposed to how each
# link is then driven. _connect_many() forwards only these to _candidates().
_ENUM_KWARGS = ("transport", "port", "host", "tcp_port", "name", "ble", "wifi",
                "baud")


def _candidates(*, transport=None, port=None, host=None, tcp_port, name=None,
                ble=None, wifi=False, baud=115200):
    """Every board matching these filters, as (factory, label, usb_chip) tuples.

    The single enumeration behind both arities: :class:`Bridge` probes this list
    and keeps the first board that handshakes, while a :class:`BridgeSet` opens
    all of them. An explicit ``transport``/``host``/``port`` is a list of one.
    """
    if transport is not None:
        return [(lambda t=transport: t, "transport",
                 getattr(transport, "usb_chip", None))]
    if host is not None:
        return [(lambda h=host, p=tcp_port: TcpTransport(h, p),
                 f"{host}:{tcp_port}", None)]
    if port is not None:
        chip = next((p.usb_chip for p in find_ports() if p.device == port), None)
        return [(lambda p=port, c=chip: SerialTransport(p, baud, usb_chip=c),
                 port, chip)]

    # A truthy transport flag *pins* that link: wifi=True is Wi-Fi only, ble=True
    # Bluetooth only, ble=False USB only. Quietly falling back to a link you
    # didn't ask for is how you end up measuring USB and calling it Bluetooth.
    want = _targets(name)
    if wifi:
        # May legitimately come back empty: dial-home boards are not
        # discoverable, they arrive on the accept socket instead.
        return _wifi_candidates(want, tcp_port)
    out = []
    if ble is not False:                # None (prefer) or True (only)
        try:
            out += _ble_candidates(want)
        except NoDeviceError:
            if ble:
                raise                   # Bluetooth only: nothing else to try
        except ImportError:
            if ble:
                raise
            log.debug("Bluetooth unavailable (pip install "
                      "'python-esp-bridge[ble]'); using USB serial")
    if not ble:                         # USB too, unless pinned to Bluetooth
        out += [(lambda p=p: SerialTransport(p.device, baud, usb_chip=p.usb_chip),
                 p.device, p.usb_chip) for p in find_ports()]
    if not out:
        raise NoDeviceError(
            "no bridge found — power the board (and flash it: docs/FIRMWARE.md), "
            "then pass port='COM5'/'/dev/ttyUSB0', ble=True or wifi=True")
    return out


def _wanted(want: tuple[str, ...] | None, d) -> bool:
    """Does this advertised / discovered board match any requested target?"""
    return want is None or any(_is(t, d.name, d.mac) for t in want)


def _wifi_candidates(want: tuple[str, ...] | None, tcp_port: int):
    # tcp_port is also where WE accept dial-home boards; 0 means "bind anywhere",
    # which is not an address a board can be reached on, so probe the standard one.
    devs = [d for d in find_wifi_devices(port=tcp_port or C.BRIDGE_LINK_PORT)
            if _wanted(want, d)]
    return [(lambda d=d: TcpTransport(d.host, d.port),
             f"wifi {d.host}:{d.port}", None) for d in devs]


def _ble_candidates(want: tuple[str, ...] | None):
    from .transports.ble import (
        BleTransport, find_ble_devices, find_ble_devices_fast)

    # BleDeviceInfo.name/.mac come from the advertised "espbridge_<identity>",
    # so they line up with Info.name/Info.mac. d.address does NOT: that is the
    # OS's handle for the radio — a CoreBluetooth UUID on macOS, and elsewhere
    # the Bluetooth MAC, which is not the base MAC the firmware reports.
    def matches(d) -> bool:
        return _wanted(want, d)

    # Fast path: stop scanning the moment a matching bridge appears instead of
    # waiting out the whole discover() window, so the common case connects in
    # well under a second. A full scan is the fallback.
    devs = find_ble_devices_fast(matches)
    if not devs:
        every = find_ble_devices()
        devs = [d for d in every if matches(d)]
        # Only one identity fits an advertisement, so what a board broadcasts may
        # not be the half you asked for: a *named* board never advertises its
        # MAC, and a freshly named one still advertises its MAC until it resets.
        # Either way the advert can't answer, so hand over every bridge in range
        # and let the post-connect check — which sees the full SYS_INFO — decide.
        # Costs one connect per board, and only when the fast match found nothing.
        if not devs and want:
            log.debug(f"no advert matched {want}; asking {len(every)} bridge(s) "
                      f"over the link")
            devs = every
    if not devs:
        what = f" named {' or '.join(want)}" if want else ""
        raise NoDeviceError(
            f"no bridge{what} found over Bluetooth — is the board powered, "
            f"in range, and flashed with BRIDGE_BLE_LINK enabled?")
    return [(lambda d=d: BleTransport(d.address),
             f"ble {d.ident or d.address}", None) for d in devs]


class Bridge:
    """Connection to a python-esp-bridge ESP32.

    >>> from espbridge import Bridge
    >>> with Bridge() as esp:         # prefer Bluetooth, fall back to USB serial
    ...     esp.gpio.mode(2, "output")
    ...     esp.gpio.write(2, 1)

    **Name a board and you get that board**, name several and you get those, name
    none and you get all of them. There is no separate call for several boards,
    and none for a Wi-Fi fleet — the selector decides how many you are talking to.

    >>> esp = Bridge("relays")                  # by stored name
    >>> esp = Bridge(mac="c0:49:ef:d0:3f:e0")   # ...or by MAC
    >>> esp = Bridge(port="COM7")               # a specific serial port
    >>> esp = Bridge(host="192.168.1.50")       # a specific Wi-Fi address
    >>>
    >>> boards = Bridge(["relays", "sensors"])  # exactly these  -> BridgeSet
    >>> boards = Bridge()               # every board found      -> BridgeSet
    >>> boards = Bridge(ble=False)      # every USB board        -> BridgeSet
    >>> boards = Bridge(wifi=True)      # every board on Wi-Fi   -> BridgeSet

    The first argument is the board's **identity**: the name you gave it with
    :meth:`set_name`, or its MAC if you never did. ``name=`` and ``mac=`` are the
    keyword spellings and both accept either form — ``mac=`` simply reads better
    when you are passing a MAC. ``espbridge scan`` lists what's attached;
    ``esp.info.ident`` is the string for a board you already hold.

    It is *only* an identity — never a COM port, never an IP address, which have
    their own keywords. Nothing is inferred from the shape of the string.

    A named board answers to its name on every link. Its MAC works too, though
    over Bluetooth that costs a moment: only one identity fits an advertisement,
    so a named board broadcasts its name, and finding it by MAC means asking the
    bridges in range over the link instead of reading the answer off the scan.

    A :class:`BridgeSet` holding exactly one board forwards attribute access to
    it, so the one-board case reads identically to a single Bridge. With several,
    an unqualified ``boards.gpio`` raises and tells you how to choose — silently
    auto-selecting is how a test suite once passed against a board that was not
    under test.

    Each transport keyword pins its link rather than merely preferring it:
    ``ble=False`` is USB only, ``ble=True`` Bluetooth only, and ``wifi=True``
    Wi-Fi only, which also accepts boards that dial home (see
    :class:`BridgeSet` and ``esp.wifi.link_setup``). Wireless links authenticate:
    the firmware default password is "espbridge", changed with
    ``EspBridge.ble.begin("...")`` / ``wifi.begin(..., password)`` in the sketch
    and passed here as ``password=``.

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

    # Naming one board — or any of these keywords — means "this one board".
    # Without one, or with a *list* of names, there is nothing to disambiguate
    # on, so __new__ returns every match instead of silently picking one.
    _SELECTORS = ("port", "host", "transport")

    def __new__(cls, name: str | list[str] | None = None, *,
                mac: str | list[str] | None = None, **kw):
        target = _selector(name, mac)
        if isinstance(target, str) or any(kw.get(k) is not None
                                          for k in cls._SELECTORS):
            return super().__new__(cls)
        # Plural: Python skips __init__ because this is not a cls instance.
        return _connect_many(**({"name": target} if target is not None else {}),
                             **kw)

    def __init__(
        self,
        name: str | list[str] | None = None,
        *,
        mac: str | list[str] | None = None,
        port: str | None = None,
        host: str | None = None,
        tcp_port: int = C.BRIDGE_LINK_PORT,
        wifi: bool = False,
        ble: bool | None = None,
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
        # Reached only for a single board; see __new__ for the plural path.
        self.timeout = timeout
        # Guards lazy sub-API creation in __getattr__ so two threads that first
        # touch e.g. esp.gpio at once can't both build (and leak) a sub-API.
        self._subapi_lock = threading.Lock()
        # request() re-sends this many times after a response timeout, so a lost
        # frame heals unnoticed. Only idempotent commands are retried (see
        # constants.NON_IDEMPOTENT).
        self.retries = retries
        self.reset_on_exit = False  # set for real only once connected (see below)
        self.info: Info | None = None
        self._password = password

        target = _selector(name, mac)
        want = _targets(target)
        candidates = _candidates(transport=transport, port=port, host=host,
                                 tcp_port=tcp_port, name=target, ble=ble,
                                 wifi=wifi, baud=baud)
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
                # Re-checked over the link on purpose: BLE adverts and Wi-Fi
                # discovery replies are broadcasts, i.e. untrusted input, and
                # SYS_INFO is the board answering for itself.
                if want is None or any(_is(t, self.info.name, self.info.mac)
                                       for t in want):
                    if upgrade_baud and getattr(self._t, "has_baud", True):
                        self._upgrade_baud(baud, target_baud)
                    self.reset_on_exit = reset_on_exit
                    return
                errors.append(f"{label}: {self.info.ident} (no match)")
                self.close()
            except (BridgeTimeoutError, ProtocolError, AuthError) as e:
                # While probing, a failure on one candidate moves on to the next;
                # with a single explicit candidate it propagates.
                self.close()
                if not probing:
                    raise
                log.debug(f"{label}: {e}")
                errors.append(f"{label}: {e}")
            except BaseException:
                self.close()
                raise
        raise NoDeviceError(
            "no matching bridge found — " + "; ".join(errors) if errors else
            "no bridge answered — nothing was found to try (a dial-home board is "
            "not discoverable; collect those with Bridge(wifi=True))")

    def _auth(self, password: str | None) -> None:
        """Authenticate a wireless link (SYS_AUTH) before the handshake."""
        self._password = password  # remembered so _reconnect can re-authenticate
        pw = (C.DEFAULT_PASSWORD if password is None else password).encode()
        try:
            self.request(C.SYS_AUTH, pw, timeout=5.0)
        except RemoteError as e:
            if e.status == C.Status.DENIED:
                raise AuthError(
                    "bridge rejected the password — check the password passed "
                    "to EspBridge.ble.begin() in the firmware"
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
        # Remembered so reset() can follow the board back down to the boot
        # baud (the firmware doesn't persist SYS_SET_BAUD) and re-upgrade.
        self._baud_base, self._baud_target = current, target
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
                self._baud_now = target
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
        """Reopen the link and redo the handshake (recovers a dead link)."""
        host = getattr(self._t, "host", None)
        if host is not None:  # TCP: reconnect to the same address
            tcp_port = self._t.port
            log.warning(f"link lost; reconnecting to {host}:{tcp_port}")
            self._t.close()
            self._reader.join(timeout=1.0)
            self._reset_state()
            self._t = TcpTransport(host, tcp_port)
            self._start_reader()
            self._auth(self._password)
            self._handshake(reset_on_open=False)
            return
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
        return (f"<{type(self).__name__} {info.chip.name} {info.ident} "
                f"fw{'.'.join(map(str, info.fw_version))}>")

    def close(self) -> None:
        """Close the link and stop the reader thread (idempotent). If the bridge
        was created with ``reset_on_exit=True``, the board is reset first. Prefer
        a ``with Bridge() as esp:`` block, which calls this automatically."""
        if self._closing:
            return
        self._closing = True
        with self._flow:  # unblock any writer waiting on the in-flight window
            self._flow.notify_all()
        if self.reset_on_exit and self._ready.is_set():
            with contextlib.suppress(Exception):
                self.request(C.SYS_RESET, timeout=1.0)
        self._t.close()
        if self._reader is not threading.current_thread():
            self._reader.join(timeout=1.0)

    def is_alive(self) -> bool:
        """False once the link is closing, or the board has stopped answering (a
        request timed out and a follow-up ping confirmed the link is gone — a
        reset, brown-out, or unplug). BridgeManager / espbridge.connect() use
        this to transparently reconnect."""
        return not self._closing and not self._link_dead

    def __enter__(self) -> Bridge:
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
        with self._flow:  # unblock any writers waiting on the in-flight window
            self._flow.notify_all()

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
        with self._handlers_lock, contextlib.suppress(ValueError):
            self._handlers.get(cmd, []).remove(callback)

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
            # fire-and-forget send() bytes written before it (the reply proves
            # the firmware consumed them).
            self._decr_window(len(data), clear_send=True)
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
                self._decr_window(len(data))
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

        "performance" (the default after every connect) = lowest latency.
        "battery" relaxes the connection interval for far fewer radio wakeups
        at ~0.3-1 s command RTT, and raises :attr:`timeout` to at least 5 s
        to keep request margin. Per-connection — reconnects start back in
        performance. Measured numbers: docs/PROTOCOL.md, "Power"."""
        modes = {"performance": 0, "battery": 1}
        if mode not in modes:
            raise ValueError(f"mode must be one of {sorted(modes)}, got {mode!r}")
        self.request(C.SYS_LINK_POWER, bytes([modes[mode]]))
        if mode == "battery" and self.timeout < 5.0:
            self.timeout = 5.0  # RTT reaches ~1 s at latency 4; keep request margin

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
        except RemoteError as e:
            if e.status is not C.Status.NOT_INIT:  # NOT_INIT = no BLE central
                raise
            applied["ble_link"] = None
        return applied

    def radio_off(self) -> None:
        """Full radio silence: Wi-Fi, ESP-NOW and the whole Bluetooth stack
        off — the quietest the chip gets, for jitter-sensitive realtime work
        over USB.

        What it sheds: every radio interrupt on core 0 (BT controller ticks,
        Wi-Fi MAC), the Bluedroid + Wi-Fi driver scheduler load, and ~110 KB
        of heap comes back. ADC2 pins (GPIO 0/2/4/12-15/25-27 on a classic
        ESP32) become permanently readable — they are blocked whenever the
        Wi-Fi driver is up.

        First tears down this session's Wi-Fi STA/AP and ESP-NOW (all cheap
        no-ops when unused), which powers the Wi-Fi driver off; then kills
        Bluetooth (advertising, BLE link, controller). Wi-Fi/ESP-NOW come
        back the moment you use them again — Bluetooth stays off until
        :meth:`reset` (its memory is released to the heap, by design).

        Raises RemoteError(BUSY) while a BLE central is connected or after
        ``esp.ble`` has been used this boot (reset first). USB sessions only."""
        for stop in (self.wifi.disconnect, self.wifi.ap_stop, self.espnow.end):
            try:
                stop()
            except RemoteError as e:
                if e.status is not C.Status.NOT_INIT:  # never started = already off
                    raise
        try:
            self.request(C.SYS_RADIO_OFF)
        except RemoteError as e:
            needs_fw(e, "radio_off() needs bridge firmware >= 0.16.0 — reflash")

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
        # The firmware boots back at the port-default baud (SYS_SET_BAUD is not
        # persisted) — follow it down or the READY event arrives as garbage,
        # then restore the fast link once the board is up.
        base = getattr(self, "_baud_base", None)
        upgraded = base is not None and getattr(self, "_baud_now", base) != base
        if upgraded:
            self._t.set_baudrate(base)
            self._baud_now = base
        if not self._ready.wait(5.0):
            raise BridgeTimeoutError("bridge did not come back after reset")
        if upgraded:
            self._upgrade_baud(base, self._baud_target)

    def set_name(self, name: str) -> None:
        """Give the board a name, persisted on it, so ``Bridge("relays")`` finds
        it on any link and you never have to read a MAC.

        Capped at :data:`constants.BRIDGE_NAME_MAX` characters, which is what
        keeps the Bluetooth advertisement — ``espbridge_<name>`` — inside the 26
        the scan response fits. A longer name would be truncated over the air and
        stop matching, so it is refused here instead.

        The advertised name updates on the board's next reset.
        """
        data = name.encode()
        if len(data) > C.BRIDGE_NAME_MAX:
            raise ValueError(
                f"name must be at most {C.BRIDGE_NAME_MAX} bytes (got "
                f"{len(data)}) — it has to fit the Bluetooth advertisement")
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
    """Every board that matched, as returned by ``Bridge()`` without a selector.

    Index it by position or by identity — ``boards[0]``, ``boards["relays"]``,
    ``boards["c0:49:ef:d0:3f:e0"]`` — the same keys :meth:`each` reports results
    under. Holding exactly one board it forwards attribute access to it, so the
    one-board case is indistinguishable from a single :class:`Bridge`.
    """

    _listener = None   # accept socket for dial-home boards; closed by close_all()

    def wait_for(self, count: int = 1, timeout: float | None = None) -> int:
        """Block until at least `count` boards are in the set; returns how many.

        Only useful for a set that is still filling up, i.e. ``Bridge(wifi=True)``
        collecting boards as they dial home.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while len(self) < count:
            if deadline is not None and time.monotonic() > deadline:
                raise BridgeTimeoutError(
                    f"only {len(self)}/{count} boards connected within {timeout:g}s")
            time.sleep(0.05)
        return len(self)

    def each(self, fn, *, workers: int = 64) -> dict[str, object]:
        """Run ``fn(bridge)`` on every board, at most `workers` at a time.

        Returns ``{mac: result}``. A board that raised maps to its exception
        instead of failing the whole sweep — across hundreds of boards some are
        always mid-reboot or out of range.

        `workers` bounds threads, not throughput: each request is a millisecond
        or ten, so 64 in flight already saturates a LAN full of boards.
        """
        from concurrent.futures import ThreadPoolExecutor

        def one(b):
            try:
                return fn(b)
            except Exception as e:  # noqa: BLE001 — reported per board
                return e

        boards = list(self)
        if not boards:
            return {}
        with ThreadPoolExecutor(max_workers=min(workers, len(boards))) as pool:
            results = pool.map(one, boards)
        return {b.info.ident if b.info else f"?{i}": r
                for i, (b, r) in enumerate(zip(boards, results))}

    def __getitem__(self, key):
        """``boards[0]`` by position, ``boards["relays"]`` by name or MAC."""
        if not isinstance(key, str):
            return super().__getitem__(key)
        for b in self:
            if b.info is not None and _is(key, b.info.name, b.info.mac):
                return b
        raise NoDeviceError(f"no connected bridge named {key!r} — connected: "
                            f"{', '.join(self.idents()) or 'none'}")

    def idents(self) -> list[str]:
        """What each connected board answers to — its name, or its MAC."""
        return [b.info.ident for b in self if b.info is not None]

    def __getattr__(self, attr: str):
        """Forward to the only board, so a one-board set reads like one Bridge.

        ``Bridge()`` returns a set, and on the overwhelmingly common one-board
        desk ``esp.gpio.write(2, 1)`` should just work. With several boards there
        is no defensible answer to "which one", so this raises and says how to
        choose — the old behaviour, silently auto-selecting, is how a test suite
        once passed 10/10 against a board that was not under test.
        """
        if attr.startswith("_"):        # never forward private/dunder lookups
            raise AttributeError(attr)
        if len(self) == 1:
            return getattr(self[0], attr)
        if not self:
            raise NoDeviceError(
                f"no boards connected, so there is no .{attr} — nothing answered "
                f"the scan yet (wait_for() if they are still dialling in)")
        found = self.idents()
        pick = f"boards[{found[0]!r}]" if found else "boards[0]"
        raise NoDeviceError(
            f"{len(self)} boards connected, so .{attr} is ambiguous: "
            f"{', '.join(found)}. Pick one — {pick} — or fan out with each(fn). "
            f"To connect to just one: Bridge('{found[0] if found else '...'}').")

    def close(self) -> None:
        """Alias for :meth:`close_all` — closing "all of them" is the only thing
        ``.close()`` can mean on a set, so it needs no disambiguation."""
        self.close_all()

    def close_all(self) -> None:
        """Close every bridge in the set (also done by the ``with`` block exit)."""
        if self._listener is not None:
            self._listener.close()
            self._listener = None    # also the accept loop's stop flag
        for b in self:
            b.close()

    def __enter__(self) -> BridgeSet:
        return self

    def __exit__(self, *exc) -> None:
        self.close_all()


def _connect_many(*, wifi=False, tcp_port: int = C.BRIDGE_LINK_PORT,
                  bind: str = "0.0.0.0", on_connect=None, backlog: int = 512,
                  **kwargs) -> BridgeSet:
    """Every board that matches, as a :class:`BridgeSet`. Reached via ``Bridge()``.

    Uses the same :func:`_candidates` enumeration the single-board path probes —
    it just opens all of them instead of stopping at the first. With ``wifi=True``
    it also accepts boards that dial home, which go on arriving for as long as the
    set is open (see :class:`Bridge` for the full description).
    """
    out = BridgeSet()

    def adopt(esp, where: str, *, incoming: bool = False) -> None:
        """Add a board, keeping exactly one live link per MAC.

        Which link survives a duplicate depends on who opened it:

        * enumeration (``incoming=False``) — one board can be reachable on
          several links at once, advertising over Bluetooth *and* sitting on a
          USB port, so the same MAC turns up twice. Candidates are enumerated in
          preference order, so the incumbent is the preferred link: keep it and
          drop the redundant newcomer.
        * dial-home (``incoming=True``) — a board connecting in is asserting a
          fresh session, which means whatever we still hold for it is stale even
          if the socket has not noticed yet. Newcomer wins, the same rule the
          firmware applies to a reconnecting host in link_tcp.cpp.
        """
        # Keyed by MAC, not by name: names are user-assigned and need not be
        # unique, the MAC always is.
        mac = esp.info.mac if esp.info else ""
        drop = [b for b in out
                if not b.is_alive() or (incoming and b.info and b.info.mac == mac)]
        for stale in drop:
            stale.close()
            out.remove(stale)
        if any(b.info and b.info.mac == mac for b in out):
            log.debug(f"{mac} already connected on a preferred link; "
                      f"dropping the redundant {where}")
            esp.close()
            return
        out.append(esp)
        log.info(f"{esp.info.ident if esp.info else mac} online from {where} "
                 f"({len(out)} boards)")
        if on_connect is not None:
            on_connect(esp)

    enum = {k: v for k, v in kwargs.items() if k in _ENUM_KWARGS}
    errors: list[str] = []
    for factory, label, _chip in _candidates(tcp_port=tcp_port, wifi=wifi, **enum):
        try:
            adopt(Bridge(transport=factory(), **kwargs), label)
        except BridgeError as e:
            log.warning(f"skipping {label}: {e}")
            errors.append(f"{label}: {e}")

    if not wifi:
        # An explicit list is a promise about which boards we drive, so a partial
        # result fails loudly: quietly running on two of the three you asked for
        # is the same class of bug as auto-selecting one out of several. (With
        # wifi=True there is nothing to judge yet — the rest may still dial in.)
        want = _targets(kwargs.get("name")) or ()
        missing = [t for t in want
                   if not any(_is(t, b.info.name, b.info.mac)
                              for b in out if b.info)]
        if missing:
            out.close_all()
            raise NoDeviceError(
                f"{len(missing)} of {len(want)} requested boards not found: "
                f"{', '.join(missing)}"
                + (" — " + "; ".join(errors) if errors else ""))
        if not out:
            raise NoDeviceError("no bridges connected"
                                + (" — " + "; ".join(errors) if errors else ""))
        return out

    # Dial-home boards: nothing to enumerate, they come to us.

    out._listener = listener = TcpListener(tcp_port, bind, backlog=backlog)
    log.info(f"accepting dial-home boards on {bind}:{listener.port}")

    def accept_loop():
        while out._listener is not None:
            transport = listener.accept()
            if transport is None:
                continue
            try:
                adopt(Bridge(transport=transport, **kwargs), transport.host,
                      incoming=True)
            except BridgeError as e:
                log.warning(f"rejected {transport.host}: {e}")
                transport.close()

    threading.Thread(target=accept_loop, daemon=True,
                     name="espbridge-accept").start()
    return out
