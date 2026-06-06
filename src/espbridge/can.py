"""CAN bus (the ESP32's TWAI controller, python-can-flavored API).

Wire a CAN transceiver (SN65HVD230, TJA1050, ...) between the chosen pins
and the bus — the ESP32 pins speak TTL, not CAN levels.

    esp.can.begin(tx=21, rx=22, bitrate=500_000)
    esp.can.send(0x123, b"\\x01\\x02")
    msg = esp.can.recv(timeout=1.0)        # polled
    esp.can.on_message(lambda m: print(m)) # or callbacks
"""
from __future__ import annotations

import queue
import struct
from dataclasses import dataclass, field

from . import constants as C

_BITRATES = {25_000: 0, 50_000: 1, 100_000: 2, 125_000: 3,
             250_000: 4, 500_000: 5, 800_000: 6, 1_000_000: 7}
_MODES = {"normal": 0, "listen": 1, "no_ack": 2}


@dataclass
class Message:
    id: int
    data: bytes = b""
    extended: bool = False
    rtr: bool = field(default=False, repr=False)

    def __str__(self) -> str:
        return f"CAN {self.id:0{8 if self.extended else 3}X} [{len(self.data)}] {self.data.hex(' ')}"


class Can:
    def __init__(self, bridge):
        self._b = bridge
        bridge.require(C.Cap.TWAI, "CAN/TWAI")
        self._rx: queue.Queue[Message] = queue.Queue(maxsize=1024)
        self._callbacks: list = []
        bridge.on_event(C.TWAI_RX_EVT, self._on_rx)

    def _on_rx(self, payload: bytes) -> None:
        msg = Message(id=struct.unpack_from(">I", payload, 1)[0],
                      data=payload[5:],
                      extended=bool(payload[0] & 1),
                      rtr=bool(payload[0] & 2))
        if self._callbacks:
            for cb in self._callbacks:
                cb(msg)
        else:
            try:
                self._rx.put_nowait(msg)
            except queue.Full:
                pass  # oldest-first backpressure: drop newest

    def begin(self, tx: int, rx: int, bitrate: int = 500_000, *,
              mode: str = "normal",
              accept: tuple[int, int] | None = None) -> None:
        """Start the controller. mode: normal | listen | no_ack.

        accept=(code, mask) programs the single acceptance filter
        (hardware-level; see the ESP32 TRM for the bit layout).
        """
        payload = bytes([tx, rx, _MODES[mode], _BITRATES[bitrate]])
        if accept is not None:
            payload += struct.pack(">IIB", accept[0], accept[1], 1)
        self._b.request(C.TWAI_INIT, payload)

    def send(self, msg_or_id: Message | int, data: bytes = b"", *,
             extended: bool = False, rtr: bool = False) -> None:
        """Queue one frame (blocks briefly if the TX queue is full)."""
        m = msg_or_id if isinstance(msg_or_id, Message) else Message(
            msg_or_id, data, extended, rtr)
        if len(m.data) > 8:
            raise ValueError("classic CAN frames carry at most 8 bytes")
        flags = (1 if m.extended else 0) | (2 if m.rtr else 0)
        self._b.request(C.TWAI_SEND,
                        struct.pack(">BI", flags, m.id) + m.data)

    def recv(self, timeout: float | None = None) -> Message | None:
        """Next received frame (None on timeout). Unused when callbacks are set."""
        try:
            return self._rx.get(timeout=timeout)
        except queue.Empty:
            return None

    def on_message(self, callback) -> None:
        """callback(Message) for every received frame (reader thread —
        don't call blocking bridge requests from inside it)."""
        self._callbacks.append(callback)

    def status(self) -> dict:
        r = self._b.request(C.TWAI_STATUS)
        state, tx_err, rx_err = r[0], r[1], r[2]
        return {"state": ("stopped", "running", "recovering", "bus_off")[state],
                "tx_errors": tx_err, "rx_errors": rx_err,
                "rx_missed": struct.unpack_from(">I", r, 3)[0]}

    def recover(self) -> None:
        """Start bus-off recovery."""
        self._b.request(C.TWAI_RECOVER)

    def end(self) -> None:
        self._b.request(C.TWAI_DEINIT)
