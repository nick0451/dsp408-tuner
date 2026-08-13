"""Whole-channel records, read-modify-write, and a journal of every intent.

One layer above :mod:`tuner.dsp.session`, which moves frames, and below a
backend, which talks in engineering units. This module owns the fact that
**the device has no partial write**.

Why read-modify-write is not optional
-------------------------------------
Every write observed on the wire carries a whole 8-byte block. The vendor app's
gain writes also carried mute, polarity, delay, EQ mode and speaker type --
values it could only have had from the connect-time bulk read. A backend that
sends a block without first reading it reverts every field it did not set, and
the reverted fields are exactly the ones nobody was thinking about.

So there is no ``write_field`` here. There is
:meth:`Dsp408Device.modify_block`, which reads, mutates, writes and verifies.

Why the journal exists
----------------------
A crash or link drop between transmitting a write and confirming it leaves the
question "did that land?" unanswerable from memory. Each intent is written and
fsynced to disk **before** the frame goes out, so a later
:meth:`Dsp408Device.reconcile` can compare the journal against a fresh readback
and classify each write as landed, not landed, or conflicting.

Cheap insurance, and it also covers the laptop dying mid-run.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .protocol import (
    BLOCK_PAYLOAD_LEN,
    UNVERIFIED_OUTPUT_BLOCKS,
    UnsendableFrame,
)
from .session import BULK_RECORD_LEN, N_OUTPUTS, Dsp408Session

#: 37 blocks of 8 bytes.
BLOCK_LEN = 8
BLOCKS_PER_RECORD = BULK_RECORD_LEN // BLOCK_LEN


class DeviceError(RuntimeError):
    """Base for record-layer failures."""


class WritesNotArmed(DeviceError):
    """A write was attempted without the preconditions being met.

    Two keys, both required: a verified snapshot on disk captured **this
    session from this device**, and an explicit :meth:`Dsp408Device.arm_writes`
    naming a reason. Neither alone is enough, because the failure this guards
    against is a script that writes when nobody intended it to.
    """


class ReadbackMismatch(DeviceError):
    """The device does not hold what we just wrote.

    Reported at whole-record granularity on purpose. Comparing only the block
    we aimed at would miss an off-by-one ``data_id`` landing the right bytes
    eight bytes away -- which is the exact failure the protocol doc warns about
    and the one a per-block check cannot see.
    """


class UnsendablePlan(DeviceError):
    """Some block in a planned write cannot be put on the wire.

    Raised by :meth:`Dsp408Device.preflight` **before anything is
    transmitted**, so a plan that would die partway through dies whole
    instead. The device has no undo and a half-written channel matches no
    model anyone reasoned about.
    """


class UnverifiedBlock(DeviceError):
    """A write targeted a block whose meaning is contradicted.

    ``DataID`` 34 and 35: the decompiled app calls 34 ``MIX_IN_9_16``, the
    device's own readback returns dynamics-shaped bytes there. Until that is
    settled, writing either sends bytes to an opcode whose destination is
    unverified, on hardware with no undo.
    """


class WriteOutcome(str, Enum):
    """Whether the device *now* holds what a journalled write intended.

    Note the tense. This answers "what does the device hold", not "did the
    transmission succeed", and the two differ in a case that comes up
    routinely: after a deliberate rollback, every write the run made reports
    ``NOT_LANDED``, because the block has been put back to its pre-write value.
    That is the correct and expected reading, not a fault.

    Reconciliation is for the run that ended *without* knowing -- a crash, a
    dropped link, a dead laptop. In that context the tense is exactly the
    question worth asking.
    """

    #: The block holds the value the entry intended to write.
    LANDED = "landed"

    #: The block holds the pre-write value. Either the write never reached the
    #: device, or something later put it back.
    NOT_LANDED = "not_landed"

    #: Neither the old value nor the new one. Something else changed it --
    #: another client, a preset recall behind our back, or a write that
    #: landed somewhere unintended.
    CONFLICTING = "conflicting"


@dataclass(frozen=True)
class SnapshotEvidence:
    """Proof that a restore point exists, presented when arming writes.

    Deliberately just a path and a digest: this module verifies the file is
    still there and still hashes the same, without knowing its format. That
    keeps :mod:`tuner.dsp.snapshot` free to depend on this module rather than
    the other way round.
    """

    path: Path
    digest: str
    firmware: str
    session_id: str

    def verify(self) -> None:
        """Raise unless the file exists and still matches its digest."""
        if not self.path.is_file():
            raise WritesNotArmed(
                f"snapshot {self.path} is gone. Capture a fresh one; do not "
                f"write without a restore point."
            )
        actual = hashlib.sha256(self.path.read_bytes()).hexdigest()
        if actual != self.digest:
            raise WritesNotArmed(
                f"snapshot {self.path} has changed since it was captured "
                f"({actual[:12]} != {self.digest[:12]}). It is no longer "
                f"evidence of anything."
            )


@dataclass(frozen=True)
class JournalEntry:
    """One intended write, recorded before it was transmitted."""

    timestamp: float
    session_id: str
    output: int
    data_id: int
    before: str
    after: str
    reason: str

    def as_json(self) -> str:
        return json.dumps(
            {
                "timestamp": self.timestamp,
                "session_id": self.session_id,
                "output": self.output,
                "data_id": self.data_id,
                "before": self.before,
                "after": self.after,
                "reason": self.reason,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, line: str) -> JournalEntry:
        return cls(**json.loads(line))


class WriteJournal:
    """Append-only record of intended writes, fsynced before transmission.

    The ordering is the whole point. A journal written *after* a successful
    write records only what already worked, which is the case that needs no
    record.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path is not None else None
        self.entries: list[JournalEntry] = []

    def append(self, entry: JournalEntry) -> None:
        self.entries.append(entry)
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(entry.as_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    @classmethod
    def load(cls, path: Path) -> WriteJournal:
        journal = cls(path)
        if Path(path).is_file():
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    journal.entries.append(JournalEntry.from_json(line))
        return journal


@dataclass(frozen=True)
class Reconciliation:
    """What a journalled write turned out to have done."""

    entry: JournalEntry
    outcome: WriteOutcome
    actual: str

    @property
    def needs_attention(self) -> bool:
        return self.outcome is not WriteOutcome.LANDED


@dataclass
class DeviceStats:
    reads: int = 0
    writes: int = 0
    verifications: int = 0
    blocks_skipped: int = 0


@dataclass
class Dsp408Device:
    """Whole-channel records over a session, with writes verified by readback."""

    session: Dsp408Session
    journal: WriteJournal = field(default_factory=lambda: WriteJournal(None))
    session_id: str = "unnamed"
    clock: Callable[[], float] = time.time

    _cache: dict[int, bytes] = field(default_factory=dict, init=False)
    _evidence: SnapshotEvidence | None = field(default=None, init=False)
    _armed_for: str | None = field(default=None, init=False)
    stats: DeviceStats = field(default_factory=DeviceStats, init=False)

    # -- reading -----------------------------------------------------------

    def refresh(self, output: int) -> bytes:
        """Read a channel's whole 296-byte record and cache it."""
        _require_output(output)
        record = self.session.read_channel_record(output)
        self._cache[output] = record
        self.stats.reads += 1
        return record

    def refresh_all(self) -> tuple[bytes, ...]:
        return tuple(self.refresh(ch) for ch in range(N_OUTPUTS))

    def record(self, output: int) -> bytes:
        """The cached record, reading it first if we do not have one.

        Callers that need certainty about *now* should use :meth:`refresh`.
        This exists so read-modify-write does not re-read a record it just
        verified.
        """
        _require_output(output)
        if output not in self._cache:
            return self.refresh(output)
        return self._cache[output]

    def block(self, output: int, data_id: int) -> bytes:
        _require_block(data_id)
        return self.record(output)[data_id * BLOCK_LEN : (data_id + 1) * BLOCK_LEN]

    # -- arming ------------------------------------------------------------

    def arm_writes(self, reason: str, evidence: SnapshotEvidence) -> None:
        """Permit writes, given a reason and a verified restore point."""
        if not reason.strip():
            raise WritesNotArmed("arming requires a reason; it goes in the journal")
        evidence.verify()
        self._evidence = evidence
        self._armed_for = reason

    def disarm(self) -> None:
        self._armed_for = None

    @property
    def armed(self) -> bool:
        return self._armed_for is not None

    def _require_armed(self) -> str:
        if self._armed_for is None or self._evidence is None:
            raise WritesNotArmed(
                "writes are not armed. Call arm_writes() with a reason and a "
                "verified snapshot. The device has no undo."
            )
        # Re-verified at every write, not just at arming: a snapshot deleted
        # or overwritten mid-run is no longer a restore point, and that is
        # precisely when it matters.
        self._evidence.verify()
        return self._armed_for

    # -- writing -----------------------------------------------------------

    def link_partners(self, output: int) -> tuple[int, ...]:
        """Other outputs sharing this one's link group, from the **device**.

        Empty when the channel is unlinked, or when its group is 0 -- 0 means
        "no group", not "group zero".

        **Measured 2026-08-09: the device does not mirror writes.** Changing a
        parameter on one half of a linked pair changes only that half; the
        vendor app keeps the pair consistent by sending two writes, ~10 ms
        apart, each a full read-modify-write of that channel's own block. So a
        backend that writes one half leaves the device disagreeing with the
        model the optimizer reasoned about -- which the improvement invariant
        would then measure against.

        Read from the cached records rather than from anything remembered: in
        two separate captures the app **wrote unlinking but never wrote
        re-linking**, so it can display a pair as linked while the device has
        the group stored as 0.
        """
        _require_output(output)
        group = self.record(output)[35 * BLOCK_PAYLOAD_LEN + 7]
        if not group:
            return ()
        return tuple(
            ch
            for ch in range(N_OUTPUTS)
            if ch != output and self.record(ch)[35 * BLOCK_PAYLOAD_LEN + 7] == group
        )

    def modify_block_mirrored(
        self,
        output: int,
        data_id: int,
        mutate: Callable[[bytes], bytes],
        reason: str = "",
    ) -> tuple[int, ...]:
        """:meth:`modify_block` on ``output`` and every link partner.

        Replicates what the vendor app does, for the case where the caller
        genuinely means "this channel and whatever is ganged to it". Each
        channel is still read-modify-written and verified individually, so a
        partner keeps its own undecoded bytes -- ``spk_type`` among them, which
        differs across a linked pair and would be destroyed by broadcasting one
        payload.

        Returns the outputs that actually changed.

        **Every member is pre-flighted before any member is written**, and that
        is not defensive tidiness -- it closes a hazard this method otherwise
        creates. ``channel_id`` is inside the frame checksum, so the *same*
        parameter value can be sendable on one member of a pair and unsendable
        on the other. Writing members in sequence then puts the first one's new
        value on the device and raises on the second, leaving the pair
        **mismatched** -- which for outputs 7 and 8, two subwoofers sharing a
        ported box, is the mechanical-failure case the gang rules exist to
        prevent, reached through the sanctioned gang API.

        Measured 2026-08-11 against the operator's device: of 601 possible
        ``gain_raw`` values for that pair, four (225, 253, 480, 508) are
        writable on exactly one member. Rare enough to never show up in
        testing, common enough for a fit to land on.
        """
        members = (output, *self.link_partners(output))

        planned: list[tuple[int, bytes]] = []
        for channel in members:
            current = self.block(channel, data_id)
            new = bytes(mutate(current))
            if len(new) != BLOCK_LEN:
                raise DeviceError(
                    f"mutate returned {len(new)} bytes; a block is {BLOCK_LEN}"
                )
            planned.append((channel, new))

        for channel, payload in planned:
            self.preflight(channel, [(data_id, payload)])

        touched = []
        for channel, payload in planned:
            if self.write_block(channel, data_id, payload, reason=reason):
                touched.append(channel)
        return tuple(touched)

    def modify_block(
        self,
        output: int,
        data_id: int,
        mutate: Callable[[bytes], bytes],
        reason: str = "",
    ) -> bool:
        """Read a block, transform it, write it back, verify. Returns whether
        anything was written.

        The only write path. There is no way to send a block without having
        read it first, because that is how fields nobody was thinking about
        get reverted.
        """
        current = self.block(output, data_id)
        new = bytes(mutate(current))
        if len(new) != BLOCK_LEN:
            raise DeviceError(
                f"mutate returned {len(new)} bytes; a block is {BLOCK_LEN}"
            )
        return self.write_block(output, data_id, new, reason=reason)

    def write_block(
        self, output: int, data_id: int, payload: bytes, reason: str = ""
    ) -> bool:
        """Write one whole block and verify the whole record afterwards.

        Returns ``False`` without transmitting if the device already holds
        these bytes. Fewest writes is fewest risks, and it makes a restore
        idempotent and safely re-runnable after a partial failure.
        """
        _require_output(output)
        _require_block(data_id)
        if data_id in UNVERIFIED_OUTPUT_BLOCKS:
            raise UnverifiedBlock(
                f"block {data_id}'s meaning is contradicted between the "
                f"decompiled app and the device's own readback; it may not be "
                f"written. See protocol.OutputBlock."
            )
        payload = bytes(payload)
        if len(payload) != BLOCK_LEN:
            raise DeviceError(
                f"block payload is {len(payload)} bytes, expected {BLOCK_LEN}"
            )

        armed_for = self._require_armed()
        before = self.block(output, data_id)
        if before == payload:
            self.stats.blocks_skipped += 1
            return False

        self._transmit_block(
            output, data_id, payload, before=before, reason=reason or armed_for
        )
        return True

    def rewrite_block_unchanged(
        self, output: int, data_id: int, reason: str = ""
    ) -> bytes:
        """Send a block's **own current bytes** back to the device.

        This is bring-up Stage 4, and it is the only sanctioned way to reach
        it: :meth:`write_block` refuses to transmit a payload the device
        already holds, which is right for a restore and exactly wrong for the
        first write this project has ever made. Returns the block sent.

        The payload is read **live** immediately beforehand rather than taken
        from the cache. That is the whole safety property: the bytes cannot
        differ from the device's own state, so a write that lands in the wrong
        place, arrives truncated, or is applied twice still cannot change what
        the channel holds. A ``force=True`` flag on :meth:`write_block` would
        have given the same effect and not the same guarantee -- it would rest
        on the caller having checked, and this rests on construction.

        **What Stage 4 can and cannot prove.** It proves the transport carries
        a fragmented multi-chunk write over live RFCOMM, that the device
        answers ``0x51`` under our pacing, and that the 296-byte record is
        unharmed afterwards. It **cannot** prove the bytes were stored, because
        they were already there -- a device that silently discarded the write
        looks identical. That is Stage 5's job, and it is why Stage 5 exists as
        a separate rung rather than being folded in here.

        The journal entry it writes has ``before == after``, which is the
        durable signature of a no-op probe. :meth:`reconcile` will therefore
        never classify one as ``NOT_LANDED``: for a no-op, landed and
        not-landed are the same readback. Correct, and a limitation, not a bug.
        """
        _require_output(output)
        _require_block(data_id)
        if data_id in UNVERIFIED_OUTPUT_BLOCKS:
            raise UnverifiedBlock(
                f"block {data_id}'s meaning is contradicted between the "
                f"decompiled app and the device's own readback; it may not be "
                f"written, not even unchanged. See protocol.OutputBlock."
            )
        armed_for = self._require_armed()
        live = self.refresh(output)
        payload = live[data_id * BLOCK_LEN : (data_id + 1) * BLOCK_LEN]
        self._transmit_block(
            output, data_id, payload, before=payload, reason=reason or armed_for
        )
        return payload

    def _transmit_block(
        self, output: int, data_id: int, payload: bytes, before: bytes, reason: str
    ) -> None:
        """Journal, send, and verify the whole record. The only path to the wire."""
        expected = bytearray(self.record(output))
        expected[data_id * BLOCK_LEN : (data_id + 1) * BLOCK_LEN] = payload

        # Journalled and fsynced BEFORE transmission. After is too late: the
        # case that needs a record is the one where we never find out.
        self.journal.append(
            JournalEntry(
                timestamp=self.clock(),
                session_id=self.session_id,
                output=output,
                data_id=data_id,
                before=before.hex(),
                after=payload.hex(),
                reason=reason,
            )
        )

        self.session.write_block(output, data_id, payload)
        self.stats.writes += 1
        self._verify(output, bytes(expected))

    def preflight(self, output: int, blocks: Sequence[tuple[int, bytes]]) -> None:
        """Raise unless every block in ``blocks`` could be transmitted.

        **Encoding can fail on the last frame of a plan**, and this device has
        no undo, so a writer that encodes as it goes leaves a channel
        half-configured: some blocks new, some old, and a model that describes
        neither. Checking the whole plan first turns that into a clean refusal
        with nothing transmitted.

        Measured 2026-08-11 on real device state. `Dsp408Spp.write_channel`
        wrote two blocks of OUT1 and then raised :class:`UnsendableFrame` on
        the third, because flattening band 3 -- ``freq`` 2514, ``bw`` 42, at
        ``bluetooth_device_id`` 4 -- produces a frame whose checksum computes
        to zero, which the vendor app never sends and this project refuses to.
        One channel in eight, found only because the rehearsal was run against
        a fake seeded from the real device rather than a uniform flat image.

        Frames are built by :meth:`Dsp408Session.block_write_frame`, the same
        method the transmitter uses, so this cannot check a different frame
        from the one that would go out.
        """
        _require_output(output)
        unsendable = []
        for data_id, payload in blocks:
            _require_block(data_id)
            if data_id in UNVERIFIED_OUTPUT_BLOCKS:
                raise UnverifiedBlock(
                    f"plan writes block {data_id}, whose meaning is "
                    f"contradicted. Refusing the whole plan."
                )
            try:
                self.session.block_write_frame(output, data_id, payload).encode()
            except UnsendableFrame as exc:
                unsendable.append((data_id, bytes(payload), str(exc)))

        if unsendable:
            detail = "; ".join(
                f"block {b} ({p.hex(' ')}): {m}" for b, p, m in unsendable
            )
            raise UnsendablePlan(
                f"output {output + 1}: {len(unsendable)} of {len(blocks)} "
                f"block(s) in this plan cannot be transmitted -- {detail}. "
                f"Nothing was written. The parameters must change: a checksum "
                f"is a function of the payload, so the same values will fail "
                f"again. Nudging one least-significant bit of frequency or "
                f"bandwidth is usually inaudible and shifts the checksum."
            )

    def apply_record(self, output: int, target: bytes, reason: str = "") -> list[int]:
        """Make a channel hold ``target``, writing only the blocks that differ.

        Returns the block indices written. Raises before transmitting anything
        if a differing block is one we may not write, so a restore never
        half-completes into a state nobody planned.
        """
        _require_output(output)
        target = bytes(target)
        if len(target) != BULK_RECORD_LEN:
            raise DeviceError(
                f"target record is {len(target)} bytes, expected {BULK_RECORD_LEN}"
            )

        changed = diff_blocks(self.refresh(output), target)
        blocked = sorted(set(changed) & UNVERIFIED_OUTPUT_BLOCKS)
        if blocked:
            raise UnverifiedBlock(
                f"output {output + 1} differs at block(s) {blocked}, whose "
                f"meaning is contradicted. Escalate rather than writing: this "
                f"is a difference nobody predicted."
            )

        written = []
        for data_id in changed:
            payload = target[data_id * BLOCK_LEN : (data_id + 1) * BLOCK_LEN]
            if self.write_block(output, data_id, payload, reason=reason):
                written.append(data_id)
        return written

    def _verify(self, output: int, expected: bytes) -> None:
        actual = self.refresh(output)
        self.stats.verifications += 1
        if actual == expected:
            return
        differing = diff_blocks(expected, actual)
        raise ReadbackMismatch(
            f"output {output + 1} does not match what was written. Blocks "
            f"{differing} differ. This is checked across the whole 296-byte "
            f"record, so an off-by-one data_id shows up here rather than "
            f"passing a per-block check."
        )

    # -- recovery ----------------------------------------------------------

    def reconcile(self, journal: WriteJournal | None = None) -> list[Reconciliation]:
        """Compare journalled intents against a fresh readback.

        For use after a crash, a link drop, or any run that ended without
        confirming its last write. Reads every channel the journal touched,
        once, and classifies each entry.
        """
        entries = (journal or self.journal).entries
        touched = sorted({e.output for e in entries})
        live = {ch: self.refresh(ch) for ch in touched}

        out = []
        for entry in entries:
            record = live[entry.output]
            start = entry.data_id * BLOCK_LEN
            actual = record[start : start + BLOCK_LEN]
            if actual.hex() == entry.after:
                outcome = WriteOutcome.LANDED
            elif actual.hex() == entry.before:
                outcome = WriteOutcome.NOT_LANDED
            else:
                outcome = WriteOutcome.CONFLICTING
            out.append(
                Reconciliation(entry=entry, outcome=outcome, actual=actual.hex())
            )
        return out


def diff_blocks(a: bytes, b: bytes) -> list[int]:
    """Indices of the 8-byte blocks that differ between two records."""
    if len(a) != len(b):
        raise DeviceError(f"records differ in length: {len(a)} vs {len(b)}")
    return [
        i
        for i in range(len(a) // BLOCK_LEN)
        if a[i * BLOCK_LEN : (i + 1) * BLOCK_LEN]
        != b[i * BLOCK_LEN : (i + 1) * BLOCK_LEN]
    ]


def describe_diff(a: bytes, b: bytes) -> list[str]:
    """Human-readable per-block differences, for an operator to eyeball."""
    from .protocol import OutputBlock

    lines = []
    for i in diff_blocks(a, b):
        try:
            name = OutputBlock(i).name
        except ValueError:
            name = f"EQ band {i}" if i < 31 else f"block {i}"
        lines.append(
            f"  block {i:3d} {name:<14} "
            f"{a[i * BLOCK_LEN : (i + 1) * BLOCK_LEN].hex(' ')} -> "
            f"{b[i * BLOCK_LEN : (i + 1) * BLOCK_LEN].hex(' ')}"
        )
    return lines


def _require_output(output: int) -> None:
    if not 0 <= output < N_OUTPUTS:
        raise DeviceError(f"output {output} outside 0..{N_OUTPUTS - 1}")


def _require_block(data_id: int) -> None:
    if not 0 <= data_id < BLOCKS_PER_RECORD:
        raise DeviceError(f"data_id {data_id} outside 0..{BLOCKS_PER_RECORD - 1}")


def records_digest(records: Sequence[bytes]) -> str:
    """Stable digest over a set of channel records."""
    h = hashlib.sha256()
    for record in records:
        h.update(record)
    return h.hexdigest()
