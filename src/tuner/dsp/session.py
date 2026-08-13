"""Request/reply discipline over a DSP-408 link.

One layer up from :mod:`tuner.dsp.transport`, which moves bytes, and one below
a backend, which talks in engineering units. This module owns the rules the
capture established about *how* the two ends take turns.

Everything here is derived from ``captures/btsnoop_hci.log``:

* **Strictly one outstanding request.** 2916 of 2916 transactions are one
  request then one reply, and writes are never pipelined.
* **Replies echo the request header bit-for-bit** -- 0 mismatches in 2916
  pairs -- so a reply is matched on its echoed tuple, never on arrival order.
* ``0xA2`` READ is answered by ``0x53`` carrying data; ``0xA1`` WRITE by
  ``0x51`` with an empty payload.
* Reads take a median of 47 ms and at most 339 ms; writes 85 ms and 354 ms.
* Pacing is reply-driven, not timer-driven: ~10 requests/s sustained for 286 s,
  with a turnaround floor of about 2 ms.
* On no reply the app retries the identical request once after ~3.2 s, then
  closes. There is no application-layer goodbye.

**The error path is specified here rather than discovered later.** ``0x52`` was
never observed, so a handler written when the first one arrives would be
written during a write, on the only unit, at the worst moment. See
:class:`FaultPolicy`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from .framing import FrameReader, chunk_20
from .protocol import (
    PRESET_NAME_LEN,
    PRESET_NAME_TRAILER,
    PRESET_SLOT_MAX,
    Ack,
    DataType,
    Frame,
    FrameType,
)
from .transport import Transport, TransportError
from .txpolicy import TxPolicy

#: Value in all 5834 captured frames, in both directions. ``Frame`` defaults it
#: to 0, which is a value the device has never been sent.
#:
#: Almost certainly a host-side index into the app's paired-device list -- an
#: artifact of the operator's phone, not a device identity -- which is why the
#: dataclass default is left alone and the session stamps it in one place
#: instead. It is inside the checksum, so 0 and 4 are different bytes on
#: the wire.
OBSERVED_BLUETOOTH_DEVICE_ID = 4

#: ``DataType 9`` channels the connect ritual reads, in the order the app read
#: them. Unknown ones keep neutral names on purpose.
SYSTEM_FIRMWARE = 4
SYSTEM_UNKNOWN_19 = 19
SYSTEM_UNKNOWN_2 = 2
SYSTEM_UNKNOWN_5 = 5
SYSTEM_UNKNOWN_6 = 6
SYSTEM_UNKNOWN_7 = 7
SYSTEM_UNKNOWN_8 = 8
SYSTEM_CURRENT_PRESET = 52
SYSTEM_PRESET_NAME = 0
SYSTEM_STATUS_POLL = 3

#: The connect ritual's system reads, in observed order.
HANDSHAKE_SYSTEM_CHANNELS: tuple[int, ...] = (
    SYSTEM_FIRMWARE,
    SYSTEM_UNKNOWN_19,
    SYSTEM_UNKNOWN_2,
    SYSTEM_UNKNOWN_5,
    SYSTEM_UNKNOWN_6,
    SYSTEM_UNKNOWN_7,
    SYSTEM_UNKNOWN_8,
    SYSTEM_CURRENT_PRESET,
)

N_PRESETS = 15
N_OUTPUTS = 8
BULK_READ_DATA_ID = 119

#: ``data_id`` addressing a whole channel record inside a preset slot.
#: Overloaded with EQ band 0; only the payload length tells them apart.
PRESET_RECORD_DATA_ID = 0
BULK_RECORD_LEN = 296

#: The device identifies itself with this. Refuse to proceed against anything
#: else -- a different model may share the framing and not the semantics.
EXPECTED_FIRMWARE_PREFIX = "MYDW-AV"


class SessionError(RuntimeError):
    """Base for transaction-layer failures."""


class ReplyTimeout(SessionError):
    """No reply arrived within the deadline, after the permitted retry."""


class SuspectedUsbArbitration(ReplyTimeout):
    """Nothing has ever replied on a link that opened cleanly.

    **Measured 2026-08-11.** With the vendor app connected over USB-B, the
    DSP-408 accepts the Bluetooth RFCOMM link at the link layer -- the socket
    opens in under a second -- and then ignores the control protocol entirely.
    No reply, no error frame, no disconnect. Detach USB and the identical
    command works first time.

    The failure signature is distinctive enough to name: the transport opened,
    the first transaction was sent, and **not one byte has ever come back**.
    An ordinary timeout mid-session means something else, and gets
    :class:`ReplyTimeout`.

    Subclasses ``ReplyTimeout`` so existing handling still catches it; the
    point is the message, not new control flow. This exists because the
    project has already paid once to learn what that silence means, and the
    code should remember it even when nobody does.
    """


class ProtocolViolation(SessionError):
    """The device replied, but not in a shape the capture ever showed."""


class DeviceRejected(SessionError):
    """The device returned ``0x52``.

    Never observed, so its meaning is unknown. The session is poisoned rather
    than retried: retrying an unknown error is how you find out what it meant
    the expensive way.
    """


class SessionPoisoned(SessionError):
    """The session hit an unexplained fault and will not transmit again."""


class ConcurrentTransaction(SessionError):
    """Two threads tried to transact at once.

    Raised rather than serialised. A blocking lock would produce correct wire
    behaviour while hiding a caller that believes it can pipeline, and that
    belief would survive into a design that cannot work on this link.
    """


class UnexpectedDevice(SessionError):
    """The firmware string is not one this project has been validated against."""


class LinkIdMismatch(SessionError):
    """The device echoed a ``bluetooth_device_id`` we did not send.

    Its own error, and raised on the *first* offending reply rather than after
    eight, because it is the single most likely way first contact fails and
    because every other symptom of it is misleading.

    ``bluetooth_device_id`` is 4 in all 5834 frames of the original capture and
    is almost certainly a host-side index into the phone's paired-device list.
    **A PC pairing plausibly is not 4.** The field sits inside the checksum and
    inside the echoed header, so if the device answers with a different value
    then :func:`_echo_key` rejects every reply, and without this check the
    operator sees eight discarded replies and "the link is out of step" -- or a
    bare timeout -- with nothing pointing at the cause.

    The fix is one argument: pass ``bluetooth_device_id`` to the session, or
    ``--link-id`` to ``tools/dsp408_probe.py``. This exception carries the value
    the device actually used, so the second attempt is informed rather than a
    guess.
    """


@dataclass(frozen=True)
class Pacing:
    """Timing policy, with every default traceable to a measurement."""

    #: Comfortably over 3x the observed maximum (354 ms for a write, 339 ms
    #: for a read). Erring long costs only latency when the link is genuinely
    #: dead; erring short causes spurious retries against a device with no
    #: undo, which is the more expensive mistake.
    reply_timeout_s: float = 1.5

    #: Observed turnaround: 2.1 ms minimum, 17.7 ms median. Comfortably above
    #: the floor, comfortably below the median.
    idle_after_reply_s: float = 0.020

    #: The app sustained 10.02 req/s for 286 s.
    max_requests_per_s: float = 10.0

    #: The app retries once, then closes.
    retries: int = 1

    #: Its observed retry interval.
    retry_delay_s: float = 3.2

    def __post_init__(self) -> None:
        if self.reply_timeout_s <= 0:
            raise ValueError("reply_timeout_s must be positive")
        if self.max_requests_per_s <= 0:
            raise ValueError("max_requests_per_s must be positive")
        if self.retries < 0:
            raise ValueError("retries must not be negative")


@dataclass(frozen=True)
class FaultPolicy:
    """What to do when the link misbehaves.

    Written before the first fault rather than after, because the lock-step
    model makes every case decidable in advance and because the alternative is
    improvising during a write to a device with no undo.

    Note what is *not* configurable: ``0x52`` never retries. That is not a
    tuning knob.
    """

    #: Retry a timed-out request once with byte-identical data. Safe because
    #: whole-block writes are idempotent -- a write carries all eight bytes and
    #: never a delta, so sending it twice lands the same state as once.
    retry_on_timeout: bool = True

    #: Stop the session on the first unexplained reply rather than continuing
    #: and hoping.
    poison_on_fault: bool = True

    #: Tolerate stale replies from a transaction that already timed out. They
    #: are discarded by echo mismatch; this bounds how many before giving up.
    max_stale_replies: int = 8


#: Shared immutable defaults. Both dataclasses are frozen, so one instance is
#: safe to share and keeps them out of mutable argument defaults.
DEFAULT_PACING = Pacing()
DEFAULT_FAULT_POLICY = FaultPolicy()


@dataclass
class SessionStats:
    """What happened on this link. Cheap, and the first thing to look at."""

    requests: int = 0
    replies: int = 0
    retries: int = 0
    timeouts: int = 0
    stale_replies: int = 0
    unsolicited: int = 0

    @property
    def clean(self) -> bool:
        return not (
            self.retries or self.timeouts or self.stale_replies or self.unsolicited
        )


@dataclass(frozen=True)
class DeviceIdentity:
    """What the connect ritual learned."""

    firmware: str
    current_preset: int
    preset_names: tuple[str, ...]
    system_blocks: dict[int, bytes]
    channels: tuple[bytes, ...]

    def linked_channels(self) -> frozenset[int]:
        """Zero-based outputs carrying a non-zero link group.

        Byte 7 of block 35. Writing to these has an unmeasured outcome, so
        :class:`~tuner.dsp.txpolicy.TxPolicy` refuses them by default.
        """
        return frozenset(
            ch for ch, rec in enumerate(self.channels) if rec[280 + 7] != 0
        )


@dataclass(frozen=True)
class IdleSurvival:
    """What an idle link did, and what that does and does not establish.

    Produced by :meth:`Dsp408Session.measure_idle_survival`. Read
    :attr:`survived` as *"the link tolerated at least this much silence"* --
    never as *"the link has no idle timeout"*.
    """

    #: The window asked for.
    requested_s: float
    #: The window actually spent silent, which is shorter if the link dropped.
    waited_s: float
    #: When the transport closed or errored on its own, if it did.
    dropped_at_s: float | None
    #: Frames the device sent unprompted. Never observed; a non-empty tuple is
    #: a new fact about the device, not noise.
    unsolicited: tuple[Frame, ...]
    #: Whether one read transaction succeeded after the window.
    probe_ok: bool
    probe_error: str | None

    @property
    def survived(self) -> bool:
        """The link stayed up **and** still answered afterwards.

        Both halves are needed. A socket that never closed but no longer
        answers is a dead link that has not noticed, and it is the failure the
        USB-arbitration session already showed this device is capable of.
        """
        return self.dropped_at_s is None and self.probe_ok

    def summary(self) -> str:
        if self.dropped_at_s is not None:
            return (
                f"link dropped on its own after {self.dropped_at_s:.1f} s of "
                f"silence (asked for {self.requested_s:.0f} s)"
            )
        if self.probe_ok:
            return (
                f"survived {self.waited_s:.1f} s of silence and answered a read "
                f"afterwards -- a LOWER BOUND, not the timeout"
            )
        return (
            f"socket stayed open through {self.waited_s:.1f} s of silence but "
            f"the read afterwards failed: {self.probe_error}"
        )


#: Signature of a reply-kind rule: request frame_type -> expected reply.
EXPECTED_REPLY: dict[int, int] = {
    int(FrameType.READ): int(Ack.DATA),
    int(FrameType.WRITE): int(Ack.RIGHT),
}


def _echo_key(frame: Frame, with_link_id: bool = True) -> tuple[int, ...]:
    """The header fields a reply echoes bit-for-bit.

    Everything except ``frame_type`` and the payload. Measured across all 2916
    request/reply pairs with zero mismatches, which is what makes equality the
    right test rather than a heuristic.

    ``with_link_id=False`` drops ``bluetooth_device_id`` and is used for one
    thing only: telling a :class:`LinkIdMismatch` apart from a genuinely stale
    reply. It is **not** a laxer match to fall back on -- a reply that differs
    in the link id is still refused, just with a message that names the cause.
    """
    key = (
        int(frame.device_id),
        int(frame.user_id),
        int(frame.data_type),
        int(frame.channel_id),
        int(frame.data_id),
        int(frame.pc_custom),
    )
    return key + (int(frame.bluetooth_device_id),) if with_link_id else key


class Dsp408Session:
    """Lock-step request/reply over a :class:`~tuner.dsp.transport.Transport`."""

    def __init__(
        self,
        transport: Transport,
        policy: TxPolicy | None = None,
        pacing: Pacing | None = None,
        faults: FaultPolicy | None = None,
        bluetooth_device_id: int = OBSERVED_BLUETOOTH_DEVICE_ID,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.policy = policy if policy is not None else TxPolicy()
        self.pacing = pacing if pacing is not None else DEFAULT_PACING
        self.faults = faults if faults is not None else DEFAULT_FAULT_POLICY
        self.bluetooth_device_id = bluetooth_device_id
        self.stats = SessionStats()
        self.poisoned: str | None = None

        # Injectable so timing behaviour is testable without real delays.
        # time.monotonic, never time.time -- a wall-clock step would make a
        # timeout fire early or never.
        self._clock = clock
        self._sleep = sleep

        self._reader = FrameReader()
        self._lock = threading.Lock()
        self._last_reply_at: float | None = None
        self._last_send_at: float | None = None

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        self.transport.open()

    def close(self) -> None:
        """Close the link. There is no application-layer goodbye to send."""
        self.transport.close()

    def __enter__(self) -> Dsp408Session:
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- transactions ------------------------------------------------------

    def transact(self, frame: Frame) -> Frame:
        """Send one request, return its reply. The only way to transmit."""
        if self.poisoned:
            raise SessionPoisoned(
                f"session was poisoned: {self.poisoned}. Re-arm deliberately "
                f"after a fresh snapshot and comparison."
            )

        if not self._lock.acquire(blocking=False):
            raise ConcurrentTransaction(
                "another transaction is in flight. This link permits exactly "
                "one outstanding request; serialise at the caller."
            )
        try:
            return self._transact_locked(frame)
        finally:
            self._lock.release()

    def _transact_locked(self, frame: Frame) -> Frame:
        frame = self._stamp(frame)
        self.policy.check(frame)
        raw = frame.encode()

        attempts = 1 + (self.pacing.retries if self.faults.retry_on_timeout else 0)
        for attempt in range(attempts):
            if attempt:
                self.stats.retries += 1
                self._sleep(self.pacing.retry_delay_s)
            self._pace()
            self._send(raw)
            self.policy.note_sent(frame)
            reply = self._await_reply(frame)
            if reply is not None:
                self._check_reply(frame, reply)
                self._last_reply_at = self._clock()
                self.stats.replies += 1
                return reply
            self.stats.timeouts += 1

        self._poison(f"no reply to {_describe(frame)} after {attempts} attempt(s)")
        detail = (
            f"no reply to {_describe(frame)} within "
            f"{self.pacing.reply_timeout_s:.2f} s, after {attempts} attempt(s)"
        )
        if self.stats.replies == 0:
            raise SuspectedUsbArbitration(
                f"{detail}. **Nothing has ever replied on this link.** The "
                f"transport opened but the device has answered nothing, which "
                f"is the measured signature of another control transport "
                f"holding the device: with the vendor app connected over "
                f"USB-B the DSP-408 accepts the RFCOMM link and then ignores "
                f"the protocol completely. Detach USB-B, close the vendor app, "
                f"and retry. If USB is already detached, the frame encoding is "
                f"not the suspect -- it is validated byte-for-byte against "
                f"5834 captured frames -- so look at the link, not the bytes."
            )
        raise ReplyTimeout(detail)

    def _stamp(self, frame: Frame) -> Frame:
        """Apply the session's link id, in exactly one place."""
        if int(frame.bluetooth_device_id) not in (0, self.bluetooth_device_id):
            raise ProtocolViolation(
                f"frame carries bluetooth_device_id "
                f"{frame.bluetooth_device_id}, session uses "
                f"{self.bluetooth_device_id}. It is inside the checksum, so "
                f"this changes the bytes on the wire."
            )
        return replace(frame, bluetooth_device_id=self.bluetooth_device_id)

    def _pace(self) -> None:
        now = self._clock()
        earliest = 0.0
        if self._last_reply_at is not None:
            earliest = max(
                earliest, self._last_reply_at + self.pacing.idle_after_reply_s
            )
        if self._last_send_at is not None:
            earliest = max(
                earliest, self._last_send_at + 1.0 / self.pacing.max_requests_per_s
            )
        if earliest > now:
            self._sleep(earliest - now)

    def _send(self, raw: bytes) -> None:
        for piece in chunk_20(raw):
            self.transport.write(piece)
        self._last_send_at = self._clock()
        self.stats.requests += 1

    def _await_reply(self, request: Frame) -> Frame | None:
        want = _echo_key(request)
        deadline = self._clock() + self.pacing.reply_timeout_s
        stale = 0

        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            data = self.transport.read(remaining)
            for received in self._reader.feed(data):
                if _echo_key(received.frame) == want:
                    return received.frame
                # Before writing it off as stale: does it match on
                # everything *except* the link id? Then this is not a late
                # reply to an old request, it is the device disagreeing with us
                # about one header byte, and saying so beats eight discards
                # followed by "the link is out of step".
                if _echo_key(received.frame, with_link_id=False) == _echo_key(
                    request, with_link_id=False
                ):
                    theirs = int(received.frame.bluetooth_device_id)
                    self._poison(f"device echoed bluetooth_device_id {theirs}")
                    raise LinkIdMismatch(
                        f"sent bluetooth_device_id "
                        f"{self.bluetooth_device_id}, device echoed {theirs}. "
                        f"Every reply will mismatch until they agree. Retry "
                        f"with bluetooth_device_id={theirs} (or "
                        f"--link-id {theirs}); it is almost certainly a "
                        f"host-side paired-device index, so a PC pairing "
                        f"differing from the phone's 4 is expected rather than "
                        f"alarming."
                    )
                # A reply whose echo does not match belongs to a transaction
                # that already timed out. Discard and keep waiting rather than
                # treating it as this request's answer -- which is exactly the
                # bug matching on arrival order would produce.
                stale += 1
                self.stats.stale_replies += 1
                if stale > self.faults.max_stale_replies:
                    self._poison(
                        f"{stale} unmatched replies while awaiting {_describe(request)}"
                    )
                    raise ProtocolViolation(
                        f"{stale} replies did not match the outstanding "
                        f"request; the link is out of step"
                    )
            # Nothing arrived and the transport did not block for the full
            # remaining time (a loopback returns instantly). Yield rather than
            # spin the CPU until the deadline.
            if not data and self._clock() < deadline:
                self._sleep(min(0.005, max(deadline - self._clock(), 0.0)))

    def _check_reply(self, request: Frame, reply: Frame) -> None:
        kind = int(reply.frame_type)
        if kind == int(Ack.ERROR):
            self._poison(f"device returned 0x52 to {_describe(request)}")
            raise DeviceRejected(
                f"device rejected {_describe(request)} with 0x52. This reply "
                f"was never observed in any capture, so its meaning is "
                f"unknown and it is NOT retried. Investigate before sending "
                f"anything else."
            )
        expected = EXPECTED_REPLY.get(int(request.frame_type))
        if expected is not None and kind != expected:
            self._poison(
                f"expected 0x{expected:02X} to {_describe(request)}, got 0x{kind:02X}"
            )
            raise ProtocolViolation(
                f"{_describe(request)} was answered with 0x{kind:02X}; every "
                f"captured reply to this request type is 0x{expected:02X}"
            )
        if int(request.frame_type) == FrameType.WRITE and reply.payload:
            raise ProtocolViolation(
                f"write ack carries {len(reply.payload)} payload bytes; every "
                f"captured 0x51 is empty"
            )

    def _poison(self, reason: str) -> None:
        if self.faults.poison_on_fault and self.poisoned is None:
            self.poisoned = reason

    # -- convenience -------------------------------------------------------

    def read_system(self, channel: int, user_id: int = 0) -> bytes:
        return self.transact(
            Frame(
                frame_type=FrameType.READ,
                data_type=DataType.SYSTEM,
                channel_id=channel,
                user_id=user_id,
            )
        ).payload

    def read_block(self, output: int, data_id: int) -> bytes:
        return self.transact(
            Frame(
                frame_type=FrameType.READ,
                data_type=DataType.OUTPUT_CHANNEL,
                channel_id=output,
                data_id=data_id,
            )
        ).payload

    def read_channel_record(self, output: int) -> bytes:
        """The whole 296-byte channel. The snapshot primitive."""
        record = self.read_block(output, BULK_READ_DATA_ID)
        if len(record) != BULK_RECORD_LEN:
            raise ProtocolViolation(
                f"bulk read of output {output + 1} returned {len(record)} "
                f"bytes, expected {BULK_RECORD_LEN}"
            )
        return record

    def block_write_frame(self, output: int, data_id: int, payload: bytes) -> Frame:
        """The frame :meth:`write_block` would send. Transmits nothing.

        Exists so a sendability pre-flight and the transmitter cannot drift
        apart. A pre-flight that builds the frame its own way is worthless:
        the field it is most likely to get wrong is ``bluetooth_device_id``,
        which is inside the checksum, so a check using the default 0 while the
        session stamps 4 asks a different question and answers it confidently.
        That mistake was made once, on 2026-08-11, while diagnosing the very
        bug this method exists to prevent.
        """
        return Frame(
            frame_type=FrameType.WRITE,
            data_type=DataType.OUTPUT_CHANNEL,
            channel_id=output,
            data_id=data_id,
            payload=bytes(payload),
            bluetooth_device_id=self.bluetooth_device_id,
        )

    def write_block(self, output: int, data_id: int, payload: bytes) -> None:
        """Write one whole 8-byte block. Read-modify-write is the caller's job."""
        self.transact(self.block_write_frame(output, data_id, payload))

    # -- presets: the only rollback that does not depend on our code ---------

    def read_preset_name(self, slot: int) -> str:
        """The name stored in ``slot``. Harmless at any slot 1-15."""
        raw = self.read_system(SYSTEM_PRESET_NAME, user_id=slot)
        return raw.split(b"\x00")[0].decode("ascii", "replace")

    def recall_preset(self, slot: int) -> tuple[bytes, ...]:
        """Load ``slot`` over the working area and return the eight records.

        **This is a sequence of reads.** Measured 2026-08-09: the vendor app
        recalls a preset purely by reading ``data_id`` 0 on channels 0-7 with
        ``user_id`` set to the slot. There is no select opcode and no write
        anywhere in the sequence -- the device answers with the slot's contents
        *and* loads them over the working area.

        It is therefore the fastest way to destroy a tune and the only
        whole-device restore that needs nothing of ours to be correct. Both
        facts follow from the same eight frames.

        The vendor app mutes the master volume either side of the reads. **We
        deliberately do not**, and the reason is worth stating: the master
        volume is a global we would then have to restore, and a session that
        dies mid-recall would leave the device silent with no record of what
        the level had been. Our stimulus is ours to stop, so the caller simply
        must not be emitting one. Do not add the mute without solving the
        restore.

        Returns the records in channel order, so a caller can verify the
        recall landed without a second round of reads.
        """
        self._require_real_slot(slot)
        records = []
        for output in range(N_OUTPUTS):
            record = self.transact(
                Frame(
                    frame_type=FrameType.READ,
                    data_type=DataType.OUTPUT_CHANNEL,
                    channel_id=output,
                    data_id=PRESET_RECORD_DATA_ID,
                    user_id=slot,
                )
            ).payload
            if len(record) != BULK_RECORD_LEN:
                raise ProtocolViolation(
                    f"preset {slot} channel {output + 1} returned "
                    f"{len(record)} bytes, expected {BULK_RECORD_LEN}. The "
                    f"working area may be half-recalled; re-read it before "
                    f"trusting anything."
                )
            records.append(record)
        return tuple(records)

    def store_preset(self, slot: int, name: str, records: Sequence[bytes]) -> None:
        """Write eight whole channel records into ``slot``, with a name.

        The safe half of the pair: storage is written, the working area is not
        touched, and no audio changes. Ordered name-then-records to match the
        capture exactly.
        """
        self._require_real_slot(slot)
        if len(records) != N_OUTPUTS:
            raise ValueError(f"need {N_OUTPUTS} records, got {len(records)}")
        for i, record in enumerate(records):
            if len(record) != BULK_RECORD_LEN:
                raise ValueError(
                    f"record {i} is {len(record)} bytes, need {BULK_RECORD_LEN}"
                )

        encoded = name.encode("ascii", "replace")[:PRESET_NAME_LEN]
        payload = encoded.ljust(PRESET_NAME_LEN, b"\x00") + bytes([PRESET_NAME_TRAILER])
        self.transact(
            Frame(
                frame_type=FrameType.WRITE,
                data_type=DataType.SYSTEM,
                channel_id=SYSTEM_PRESET_NAME,
                user_id=slot,
                payload=payload,
            )
        )
        for output, record in enumerate(records):
            self.transact(
                Frame(
                    frame_type=FrameType.WRITE,
                    data_type=DataType.OUTPUT_CHANNEL,
                    channel_id=output,
                    data_id=PRESET_RECORD_DATA_ID,
                    user_id=slot,
                    payload=bytes(record),
                )
            )

    @staticmethod
    def _require_real_slot(slot: int) -> None:
        if not 1 <= slot <= PRESET_SLOT_MAX:
            raise ValueError(
                f"preset slot {slot} is outside 1-{PRESET_SLOT_MAX}. Reading a "
                f"name from 7-15 succeeds, which is what made fifteen look "
                f"real; they return one identical stale string and store "
                f"nothing."
            )

    def poll_status(self) -> bytes:
        """The ~10 Hz status block the app polls continuously.

        Byte-identical 2864 times in the capture, so it carries no information
        there. Whether the link needs it to stay up is untested -- **do not
        assume it is a required keepalive, and do not assume it is not.**
        """
        return self.read_system(SYSTEM_STATUS_POLL)

    def measure_idle_survival(self, seconds: float) -> IdleSurvival:
        """Sit silent on an open link for ``seconds``, then see if it still works.

        This is bring-up Stage 2, and it answers a question the session layer
        currently *embodies* rather than knows: the vendor app polls
        ``dt9/ch3`` at ~10 Hz for the whole of its 286-second capture, so we
        have no evidence whether that traffic is a keepalive the link requires
        or merely a UI refresh. Our pacing assumes it is not required. That
        assumption first bites during a **tuning run**, where a long acoustic
        sweep is minutes of dead air on an open socket -- not at first write.

        Deliberately passive. Probing during the window would supply exactly
        the traffic being tested for. So: watch the transport for an
        unsolicited close, and only afterwards attempt one read.

        **A single call gives a lower bound, never the timeout.** "Survived
        120 s" is the whole result; the timeout, if there is one, is somewhere
        above it. Ladder the window across separate runs to bracket it.

        Anything the device sends unprompted is framed and reported rather than
        discarded -- it has never done so in 5834 frames, so it would be a new
        fact about the device, and swallowing it here is how it stays unknown.
        """
        if seconds <= 0:
            raise ValueError("idle window must be positive")
        if self.poisoned:
            raise SessionPoisoned(f"session was poisoned: {self.poisoned}")

        start = self._clock()
        dropped_at: float | None = None
        unsolicited: list[Frame] = []

        while True:
            elapsed = self._clock() - start
            if elapsed >= seconds:
                break
            if not self.transport.is_open:
                dropped_at = elapsed
                break
            try:
                data = self.transport.read(min(0.25, seconds - elapsed))
            except TransportError:
                dropped_at = self._clock() - start
                break
            for received in self._reader.feed(data):
                unsolicited.append(received.frame)
            if not data:
                self._sleep(min(0.05, max(seconds - (self._clock() - start), 0.0)))

        waited = self._clock() - start

        probe_ok = False
        probe_error: str | None = None
        if dropped_at is None:
            try:
                self.read_system(SYSTEM_FIRMWARE)
                probe_ok = True
            except (SessionError, TransportError) as exc:
                probe_error = f"{type(exc).__name__}: {exc}"

        return IdleSurvival(
            requested_s=seconds,
            waited_s=waited,
            dropped_at_s=dropped_at,
            unsolicited=tuple(unsolicited),
            probe_ok=probe_ok,
            probe_error=probe_error,
        )

    def handshake(self) -> DeviceIdentity:
        """Replay the vendor app's connect ritual, in its observed order.

        Replicated exactly rather than trimmed to what we need. If any of it
        drives a device-side state machine we get that for free, and every
        deviation from the capture is unvalidated. It costs 5.8 s.
        """
        system: dict[int, bytes] = {}
        for channel in HANDSHAKE_SYSTEM_CHANNELS:
            system[channel] = self.read_system(channel)

        firmware = system[SYSTEM_FIRMWARE].decode("ascii", "replace")
        if not firmware.startswith(EXPECTED_FIRMWARE_PREFIX):
            raise UnexpectedDevice(
                f"device reports {firmware!r}; this project has only been "
                f"validated against {EXPECTED_FIRMWARE_PREFIX}*. A different "
                f"model may share the framing and not the semantics."
            )

        slot_bytes = system[SYSTEM_CURRENT_PRESET]
        current = slot_bytes[0] if slot_bytes else 0

        names = []
        for slot in range(1, N_PRESETS + 1):
            raw = self.read_system(SYSTEM_PRESET_NAME, user_id=slot)
            names.append(raw.split(b"\x00")[0].decode("ascii", "replace"))

        channels = tuple(self.read_channel_record(ch) for ch in range(N_OUTPUTS))

        identity = DeviceIdentity(
            firmware=firmware,
            current_preset=current,
            preset_names=tuple(names),
            system_blocks=system,
            channels=channels,
        )
        # Teach the policy which outputs are linked, so writes to them are
        # refused until the mirroring question is settled by measurement.
        self.policy.set_linked_channels(identity.linked_channels())
        return identity


def _describe(frame: Frame) -> str:
    return (
        f"[0x{int(frame.frame_type):02X} dt={int(frame.data_type)} "
        f"ch={int(frame.channel_id)} did={int(frame.data_id)}]"
    )
