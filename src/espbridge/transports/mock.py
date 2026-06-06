"""In-memory transport for hardware-free tests."""
from __future__ import annotations


class MockTransport:
    """In-memory transport for hardware-free tests.

    `responder(data: bytes)` is called with everything the host writes; it can
    push firmware->host bytes back via `inject()`.
    """

    has_baud = True    # exercises the baud-upgrade code path in tests
    needs_auth = False  # tests flip this to emulate the wireless link

    def __init__(self, responder=None):
        import queue

        self.usb_chip = None
        self._rx: queue.Queue[bytes] = queue.Queue()
        self.responder = responder
        self.closed = False
        self.baud = 115200

    def inject(self, data: bytes) -> None:
        self._rx.put(data)

    def read(self) -> bytes:
        import queue

        try:
            return self._rx.get(timeout=0.05)
        except queue.Empty:
            return b""

    def write(self, data: bytes) -> None:
        if self.responder is not None:
            self.responder(data)

    def set_baudrate(self, baud: int) -> None:
        self.baud = baud

    def pulse_reset(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
