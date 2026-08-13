"""Moving bytes to and from a DSP-408. Nothing else.

This layer is deliberately the thinnest in the stack, because it is the only
one that cannot be tested without hardware. Every decision that could live
somewhere testable does: framing in :mod:`tuner.dsp.framing`, transactions in
:mod:`tuner.dsp.session`, permission in :mod:`tuner.dsp.txpolicy`. What is left
here is open, close, write, read.

Four implementations:

* :class:`LoopbackTransport` -- wires a :class:`~tuner.dsp.fake_device.FakeDsp408`
  straight to the session layer, in process. The default for tests and for
  ``--fake`` dry runs.
* :class:`ReplayTransport` -- answers from a recorded capture and refuses to
  answer anything the capture did not contain. The strict oracle.
* :class:`RfcommSocketTransport` -- a real Bluetooth RFCOMM socket. Works on
  Linux and on Windows, both of which expose ``AF_BLUETOOTH`` with
  ``BTPROTO_RFCOMM``.
* :class:`SerialPortTransport` -- a COM port bound to an outgoing Bluetooth SPP
  service, for hosts where the socket path is unavailable. Needs ``pyserial``,
  which is imported lazily and is not a hard dependency.

**The read contract is "whatever has arrived, within the timeout".** A transport
never returns frames, never waits for a whole one, and never raises on an empty
read -- RFCOMM is a byte stream and the boundary between frames is not visible
here. Deciding when enough has arrived is the framing layer's job.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType

#: SPP server channel the vendor app uses, measured from the capture's RFCOMM
#: DLCI 2 rather than read from an SDP record. Discover the channel at connect
#: time where possible and treat a different answer as worth stopping for.
DEFAULT_RFCOMM_CHANNEL = 1

#: Read buffer size. Larger than any frame so a bulk reply can arrive in one
#: call, though nothing depends on it doing so.
READ_CHUNK = 4096


class TransportError(RuntimeError):
    """The link failed. Not a protocol problem -- a plumbing one."""


class NotConnected(TransportError):
    """Raised when a transport is used before ``open`` or after ``close``."""


class Transport(ABC):
    """A bidirectional byte pipe to a device."""

    @abstractmethod
    def open(self) -> None:
        """Establish the link. Idempotent."""

    @abstractmethod
    def close(self) -> None:
        """Tear the link down. Idempotent, and safe to call after a failure."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Send bytes. Must not return until they are handed to the OS."""

    @abstractmethod
    def read(self, timeout_s: float) -> bytes:
        """Return whatever has arrived, waiting at most ``timeout_s``.

        Returns ``b""`` if nothing arrived. **An empty read is not an error**
        and must not raise: a device that is merely slow is indistinguishable
        here from one that is finished, and only the transaction layer knows
        which it was expecting.
        """

    @property
    @abstractmethod
    def is_open(self) -> bool:
        """Whether the link is currently usable."""

    def __enter__(self) -> Transport:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class LoopbackTransport(Transport):
    """Wires a fake device directly to the session layer, in process.

    No sockets, no threads, no timing. ``read`` returns whatever the fake
    queued in response to the last write, which makes the whole stack testable
    with no hardware -- the requirement in ``CLAUDE.md`` that the suite must
    pass with no DSP connected.
    """

    def __init__(self, device) -> None:
        self._device = device
        self._rx = bytearray()
        self._open = False

    @property
    def device(self):
        """The fake behind this transport, for assertions and fault injection."""
        return self._device

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True
        self._device.connect()

    def close(self) -> None:
        if self._open:
            self._device.disconnect()
        self._open = False
        self._rx.clear()

    def write(self, data: bytes) -> None:
        if not self._open:
            raise NotConnected("transport is closed")
        self._rx += self._device.feed(bytes(data))

    def read(self, timeout_s: float) -> bytes:
        if not self._open:
            raise NotConnected("transport is closed")
        out, self._rx = bytes(self._rx), bytearray()
        if out:
            # Tells the fake the host has caught up, which is how it polices
            # lock-step. Without it the fake cannot distinguish a host that
            # waits for each reply from one that pipelines and reads later.
            self._device.note_reply_read()
        return out


class ReplayTransport(Transport):
    """Answers from a recorded session, and only exactly as recorded.

    The strict oracle: the *k*-th write must equal the *k*-th captured request
    byte for byte, or it raises. That turns "our connect ritual resembles the
    vendor app's" into "our connect ritual is indistinguishable from the vendor
    app's on the wire", which is a much stronger claim and the one worth making
    before anything transmits for real.

    Writes are matched after reassembly, so the caller's chunking does not have
    to match the capture's -- ``chunk_20`` is checked separately, against the
    raw stream, in ``tests/test_framing_replay.py``.
    """

    def __init__(self, exchanges: list[tuple[bytes, bytes]]) -> None:
        self._exchanges = list(exchanges)
        self._index = 0
        self._pending = bytearray()
        self._rx = bytearray()
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def consumed(self) -> int:
        """How many recorded exchanges have been used."""
        return self._index

    @property
    def remaining(self) -> int:
        return len(self._exchanges) - self._index

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes) -> None:
        if not self._open:
            raise NotConnected("transport is closed")
        self._pending += data
        self._match()

    def read(self, timeout_s: float) -> bytes:
        if not self._open:
            raise NotConnected("transport is closed")
        out, self._rx = bytes(self._rx), bytearray()
        return out

    def _match(self) -> None:
        # Trailing zeros are the 20-byte chunk padding; they are not part of
        # the request and the capture's own frames do not include them.
        while self._pending:
            if self._index >= len(self._exchanges):
                raise TransportError(
                    f"sent {len(self._pending)} bytes past the end of the "
                    f"recording: {bytes(self._pending[:32]).hex(' ')}"
                )
            request, reply = self._exchanges[self._index]
            trimmed = bytes(self._pending).rstrip(b"\x00")
            if len(trimmed) < len(request):
                return  # more chunks still to come
            if trimmed[: len(request)] != request:
                raise TransportError(
                    f"exchange {self._index}: sent bytes do not match the "
                    f"recording.\n  expected {request.hex(' ')}\n  sent     "
                    f"{trimmed[: len(request)].hex(' ')}"
                )
            self._index += 1
            self._rx += reply
            del self._pending[:]
            leftover = trimmed[len(request) :]
            self._pending += leftover


class RfcommSocketTransport(Transport):
    """A real classic-Bluetooth RFCOMM socket.

    ``AF_BLUETOOTH`` with ``BTPROTO_RFCOMM`` is available on Linux and on
    Windows, so one implementation covers both. The device must already be
    paired; this does not pair it.

    Untested against hardware -- nothing in this project has transmitted.
    """

    def __init__(self, address: str, channel: int = DEFAULT_RFCOMM_CHANNEL) -> None:
        self._address = address
        self._channel = channel
        self._sock = None

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def open(self) -> None:
        if self._sock is not None:
            return
        import socket

        if not hasattr(socket, "BTPROTO_RFCOMM"):
            raise TransportError(
                "this Python build has no BTPROTO_RFCOMM; use "
                "SerialPortTransport with an outgoing Bluetooth COM port"
            )
        sock = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
        )
        try:
            sock.connect((self._address, self._channel))
        except OSError as exc:
            sock.close()
            raise TransportError(
                f"could not open RFCOMM channel {self._channel} on "
                f"{self._address}: {exc}"
            ) from exc
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def write(self, data: bytes) -> None:
        if self._sock is None:
            raise NotConnected("socket is not open")
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise TransportError(f"write failed: {exc}") from exc

    def read(self, timeout_s: float) -> bytes:
        if self._sock is None:
            raise NotConnected("socket is not open")
        self._sock.settimeout(max(timeout_s, 0.0))
        try:
            return self._sock.recv(READ_CHUNK)
        except TimeoutError:
            return b""
        except OSError as exc:
            raise TransportError(f"read failed: {exc}") from exc


class SerialPortTransport(Transport):
    """An outgoing Bluetooth SPP COM port, via ``pyserial``.

    The fallback where the socket path is unavailable. ``pyserial`` is imported
    lazily and is not a hard dependency, because the socket transport covers
    both target platforms and nothing should have to install a serial library
    to run the test suite.

    .. note::
       Nothing contracts a 1:1 mapping between a ``write`` call here and one
       RFCOMM frame on the air; the stack may coalesce. Since RFCOMM is a byte
       stream the device must already frame from it, so the risk is low -- but
       it is an assumption, not a guarantee, and it is why 20-byte chunking is
       replicated rather than relied upon.

    Untested against hardware.
    """

    def __init__(self, port: str, write_timeout_s: float = 2.0) -> None:
        self._port = port
        self._write_timeout_s = write_timeout_s
        self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None

    def open(self) -> None:
        if self._serial is not None:
            return
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - depends on the host
            raise TransportError(
                "SerialPortTransport needs pyserial: pip install pyserial. "
                "RfcommSocketTransport needs no extra package."
            ) from exc
        try:
            self._serial = serial.Serial(
                self._port, timeout=0, write_timeout=self._write_timeout_s
            )
        except Exception as exc:
            raise TransportError(f"could not open {self._port}: {exc}") from exc

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def write(self, data: bytes) -> None:
        if self._serial is None:
            raise NotConnected("port is not open")
        try:
            self._serial.write(data)
            self._serial.flush()
        except Exception as exc:
            raise TransportError(f"write failed: {exc}") from exc

    def read(self, timeout_s: float) -> bytes:
        if self._serial is None:
            raise NotConnected("port is not open")
        try:
            self._serial.timeout = max(timeout_s, 0.0)
            waiting = self._serial.in_waiting
            return self._serial.read(waiting or 1)
        except Exception as exc:
            raise TransportError(f"read failed: {exc}") from exc
