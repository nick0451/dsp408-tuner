"""Capturing device state, and putting it back.

This is the mechanism behind the improvement invariant's rollback clause. Until
now that clause was a policy with nothing implementing it -- an adversarial
review's sharpest finding, and a fair one.

What a snapshot is
------------------
Eight 296-byte channel records, **stored verbatim**, plus the system blocks and
preset names the connect ritual returns. Never decoded fields.

That is not caution for its own sake. Blocks 34 and 35 are undecoded and their
meaning is contradicted; ``spk_type``, ``h_filter`` and ``threshold`` have
unknown encodings; ``mute`` may really be an inverted ``enable``. A snapshot
built from parsed fields would either lose what it could not represent or
re-encode it from a guess. Raw bytes round-trip everything we do not
understand, which is most of what makes a restore worth having.

Honesty about what this does and does not cover
-----------------------------------------------
Restore writes known-good bytes back through the same path whose failure it
would be recovering from. That objection is real, and it is **partially**
valid. Broken out by failure class:

========================================  ==========================
Failure                                   Does restore help?
========================================  ==========================
Wrong values written; path works          **Yes, fully.** The common case.
Partial run -- crash, drop, abort         **Yes**, via the journal.
Transport broken / writes ignored         **No, and no damage** -- if we
                                          cannot write, we have not written.
A write corrupts state writes can't fix   **No.** Residual risk.
MCU bricked                               **No.**
========================================  ==========================

Against the last two the defences are preventive only: the transmit allow-list,
the ``.DDP`` route through the vendor app, an operator-saved preset slot,
readback verification, blast-radius caps and two-key arming. **None of them
recovers from a bricking write.** Said plainly here so no caller mistakes a
green restore for a guarantee.

One more hole, by design: **block-by-block restore cannot repair blocks 34 or
35.** They are refused on every write path because their meaning is
contradicted between the decompiled app and the device's own readback, so if
anything ever corrupts them, :func:`restore` will detect the difference and
stop rather than fix it. The two paths that *can* are
:func:`restore_from_preset` and a ``.DDP`` load through the vendor app, both of
which replace whole records without needing to understand them. That is the
right trade — refusing to write bytes we cannot interpret is worth more than
being able to repair them — but it should be a known limit rather than a
surprise mid-recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import ddp
from .device import (
    BLOCK_LEN,
    Dsp408Device,
    SnapshotEvidence,
    diff_blocks,
)
from .protocol import UNVERIFIED_OUTPUT_BLOCKS
from .session import BULK_RECORD_LEN, N_OUTPUTS, DeviceIdentity

SCHEMA = "dsp408-snapshot/1"


class SnapshotError(RuntimeError):
    """Base for snapshot and restore failures."""


class SnapshotCorrupt(SnapshotError):
    """A stored snapshot does not match its own digest, or is malformed."""


class RestoreBlocked(SnapshotError):
    """A restore stopped before writing rather than proceeding unsafely."""


@dataclass(frozen=True)
class SnapshotProvenance:
    """Where a snapshot came from. Not bookkeeping -- comparability."""

    captured_utc: str
    session_id: str
    firmware: str
    transport: str
    tool_version: str = SCHEMA
    notes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DeviceSnapshot:
    """A complete, restorable picture of the device's working area."""

    channels: tuple[bytes, ...]
    system_blocks: dict[int, bytes]
    preset_names: tuple[str, ...]
    current_preset: int
    provenance: SnapshotProvenance

    def __post_init__(self) -> None:
        if len(self.channels) != N_OUTPUTS:
            raise SnapshotError(
                f"snapshot holds {len(self.channels)} channels, expected {N_OUTPUTS}"
            )
        for i, record in enumerate(self.channels):
            if len(record) != BULK_RECORD_LEN:
                raise SnapshotError(
                    f"channel {i} record is {len(record)} bytes, expected "
                    f"{BULK_RECORD_LEN}"
                )

    @property
    def digest(self) -> str:
        """Digest over the channel records only.

        Deliberately not over the provenance: two snapshots of the same device
        state taken a minute apart should compare equal, and they would not if
        the timestamp were folded in.
        """
        h = hashlib.sha256()
        for record in self.channels:
            h.update(record)
        return h.hexdigest()

    def block(self, output: int, data_id: int) -> bytes:
        return self.channels[output][data_id * BLOCK_LEN : (data_id + 1) * BLOCK_LEN]

    def linked_channels(self) -> frozenset[int]:
        return frozenset(
            ch for ch, rec in enumerate(self.channels) if rec[280 + 7] != 0
        )

    # -- persistence -------------------------------------------------------

    def to_json(self) -> str:
        body = {
            "schema": SCHEMA,
            "digest": self.digest,
            "current_preset": self.current_preset,
            "preset_names": list(self.preset_names),
            "channels": [r.hex() for r in self.channels],
            "system_blocks": {
                str(k): v.hex() for k, v in sorted(self.system_blocks.items())
            },
            "provenance": {
                "captured_utc": self.provenance.captured_utc,
                "session_id": self.provenance.session_id,
                "firmware": self.provenance.firmware,
                "transport": self.provenance.transport,
                "tool_version": self.provenance.tool_version,
                "notes": self.provenance.notes,
            },
        }
        return json.dumps(body, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> DeviceSnapshot:
        body = json.loads(text)
        schema = body.get("schema")
        if schema != SCHEMA:
            raise SnapshotCorrupt(
                f"snapshot schema {schema!r}, this build writes {SCHEMA!r}"
            )
        prov = body["provenance"]
        snapshot = cls(
            channels=tuple(bytes.fromhex(h) for h in body["channels"]),
            system_blocks={
                int(k): bytes.fromhex(v)
                for k, v in body.get("system_blocks", {}).items()
            },
            preset_names=tuple(body.get("preset_names", ())),
            current_preset=body.get("current_preset", 0),
            provenance=SnapshotProvenance(
                captured_utc=prov["captured_utc"],
                session_id=prov["session_id"],
                firmware=prov["firmware"],
                transport=prov["transport"],
                tool_version=prov.get("tool_version", SCHEMA),
                notes=prov.get("notes", {}),
            ),
        )
        stored = body.get("digest")
        if stored != snapshot.digest:
            raise SnapshotCorrupt(
                f"digest mismatch: file says {stored}, contents hash to "
                f"{snapshot.digest}. Do not restore from this."
            )
        return snapshot

    def save(self, path: Path) -> SnapshotEvidence:
        """Write atomically and return proof suitable for arming writes."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_json().encode("utf-8")

        # Written to a temp file and replaced, so a crash mid-write cannot
        # leave a truncated snapshot that looks loadable.
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

        return SnapshotEvidence(
            path=path,
            digest=hashlib.sha256(path.read_bytes()).hexdigest(),
            firmware=self.provenance.firmware,
            session_id=self.provenance.session_id,
        )

    @classmethod
    def load(cls, path: Path) -> DeviceSnapshot:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def evidence_for(cls, path: Path) -> SnapshotEvidence:
        """Evidence for a snapshot already on disk, without rewriting it.

        Loading validates the digest, so a file that reaches here is a real
        restore point. Rewriting it to obtain evidence would be the wrong
        shape of operation entirely: the point of a restore point is that it
        does not change.
        """
        path = Path(path)
        shot = cls.load(path)
        return SnapshotEvidence(
            path=path,
            digest=hashlib.sha256(path.read_bytes()).hexdigest(),
            firmware=shot.provenance.firmware,
            session_id=shot.provenance.session_id,
        )

    # -- interoperability --------------------------------------------------

    def to_ddp(self, template: ddp.DdpBackup) -> bytes:
        """A ``.DDP`` the **vendor app** can load, carrying these records.

        The second restore path, and the one that matters most if ours is the
        thing that broke: it shares no code with our writer. The template
        supplies the global and input sections, which are carried through
        undecoded, so it must come from the same device.

        Load one of these into the vendor app once, as a rehearsal, before
        relying on it.
        """
        return ddp.splice_outputs(template, self.channels)


@dataclass(frozen=True)
class ChannelRestore:
    """What a restore did, or would do, to one channel."""

    output: int
    differing_blocks: tuple[int, ...]
    written_blocks: tuple[int, ...]
    verified: bool
    blocked: tuple[int, ...] = ()

    @property
    def already_matched(self) -> bool:
        return not self.differing_blocks


@dataclass(frozen=True)
class RestoreReport:
    """Outcome of a whole restore."""

    channels: tuple[ChannelRestore, ...]
    dry_run: bool

    @property
    def total_writes(self) -> int:
        return sum(len(c.written_blocks) for c in self.channels)

    @property
    def clean(self) -> bool:
        """Every channel now matches the snapshot, and nothing was blocked.

        For a dry run this is the same question as :attr:`device_matches`:
        a dry run that found differences is *not* clean, because the device
        does not hold the snapshot. It reports a fact about the device, never
        about whether the code ran without error.
        """
        return all(c.verified or c.already_matched for c in self.channels) and not any(
            c.blocked for c in self.channels
        )

    @property
    def blocks_to_write(self) -> int:
        """Blocks that differ from the snapshot, written or not."""
        return sum(len(c.differing_blocks) for c in self.channels)

    @property
    def device_matches(self) -> bool:
        """Whether the device already holds the snapshot, with nothing to do."""
        return all(c.already_matched for c in self.channels)

    def summary(self) -> str:
        if self.dry_run:
            head = f"restore DRY RUN: {self.blocks_to_write} block(s) would change"
        else:
            head = f"restore applied: {self.total_writes} block write(s)"
        lines = [head]
        for c in self.channels:
            label = f"  OUT{c.output + 1}:"
            if c.blocked:
                lines.append(f"{label} BLOCKED at {list(c.blocked)}")
            elif c.already_matched:
                lines.append(f"{label} already matches")
            elif self.dry_run:
                lines.append(
                    f"{label} would write {len(c.differing_blocks)} block(s) "
                    f"{list(c.differing_blocks)}"
                )
            else:
                state = "verified" if c.verified else "NOT VERIFIED"
                lines.append(
                    f"{label} {len(c.written_blocks)} block(s) written, {state}"
                )
        return "\n".join(lines)


def capture(
    device: Dsp408Device,
    identity: DeviceIdentity,
    transport_name: str = "unknown",
    notes: dict[str, str] | None = None,
    now: float | None = None,
) -> DeviceSnapshot:
    """Read the whole device and package it.

    ``identity`` comes from :meth:`~tuner.dsp.session.Dsp408Session.handshake`,
    which already read every channel; the records are re-read here anyway so a
    snapshot always reflects the moment it was taken rather than the moment the
    session opened.
    """
    records = device.refresh_all()
    stamp = time.gmtime(now if now is not None else time.time())
    return DeviceSnapshot(
        channels=records,
        system_blocks=dict(identity.system_blocks),
        preset_names=tuple(identity.preset_names),
        current_preset=identity.current_preset,
        provenance=SnapshotProvenance(
            captured_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", stamp),
            session_id=device.session_id,
            firmware=identity.firmware,
            transport=transport_name,
            notes=dict(notes or {}),
        ),
    )


def compare(device: Dsp408Device, snapshot: DeviceSnapshot) -> dict[int, list[int]]:
    """Blocks where the live device differs from a snapshot, **per output**.

    Read-only. Detects a preset recall that happened behind our back, among
    other things.

    **Output records only.** Global state -- the master volume above all -- is
    captured in the snapshot but not compared here, because this returns a
    per-channel map and globals have no channel. Use :func:`compare_system`,
    and see its docstring for why ignoring it would undermine the improvement
    invariant rather than merely being incomplete.
    """
    out = {}
    for ch in range(N_OUTPUTS):
        differing = diff_blocks(device.refresh(ch), snapshot.channels[ch])
        if differing:
            out[ch] = differing
    return out


#: ``DataType 9`` channel carrying the master volume, measured 2026-08-09.
#: Byte 0 is the level; the app writes 0 there and restores it around a preset
#: recall, which is how it was identified.
SYSTEM_MASTER_VOLUME = 5


def compare_system(
    device: Dsp408Device, snapshot: DeviceSnapshot
) -> dict[int, tuple[bytes, bytes]]:
    """System blocks where the live device differs from a snapshot.

    Returns ``{channel: (snapshot_bytes, live_bytes)}``.

    **This exists because of the master volume.** It lives at ``DataType 9``
    channel 5, it is **global** -- one write moves every output at once -- and
    it is not in any channel record. It was captured by the handshake all along
    and compared by nothing.

    Why that matters more than it sounds: the improvement invariant compares a
    baseline measurement against a verification measurement and attributes the
    difference to the tune. If the master volume moved between them -- someone
    nudged the head unit, the vendor app was opened, a preset recall restored a
    different level -- then the two measurements are not commensurable, the
    difference is not acoustic, and **provenance would not catch it because
    provenance did not record it.**

    That is the same class of failure as an uncomparable microphone calibration,
    and the invariant's answer to it is the same: the result is *indeterminate*,
    not pass or fail. An orchestration run should read this at baseline and
    again at verification, and refuse on any difference.

    Whether a preset recall restores the master volume is **unobserved**; the
    app writes it explicitly around one, which suggests not, but that is an
    inference from the app's caution rather than a measurement.
    """
    out: dict[int, tuple[bytes, bytes]] = {}
    for channel, stored in sorted(snapshot.system_blocks.items()):
        live = device.session.read_system(channel)
        if live != stored:
            out[channel] = (stored, live)
    return out


def restore(
    device: Dsp408Device,
    snapshot: DeviceSnapshot,
    outputs: Sequence[int] | None = None,
    dry_run: bool = True,
    reason: str = "rollback",
) -> RestoreReport:
    """Put the device back to ``snapshot``, one channel at a time.

    Defaults to ``dry_run=True``. A restore that writes by default is a footgun
    on hardware with no undo, and the dry run is also how an operator sees what
    would change before agreeing to it.

    Per channel: read the live record, diff block by block, refuse outright if
    a differing block is one we may not write, write only the blocks that
    differ, and verify the **whole 296-byte record** afterwards. Blocks that
    already match are not rewritten -- fewest writes is fewest risks, and it
    makes this idempotent and safely re-runnable after a partial failure.
    """
    targets = list(range(N_OUTPUTS)) if outputs is None else list(outputs)
    for ch in targets:
        if not 0 <= ch < N_OUTPUTS:
            raise SnapshotError(f"output {ch} outside 0..{N_OUTPUTS - 1}")

    results = []
    for ch in targets:
        live = device.refresh(ch)
        target = snapshot.channels[ch]
        differing = diff_blocks(live, target)
        blocked = tuple(sorted(set(differing) & UNVERIFIED_OUTPUT_BLOCKS))

        if blocked:
            # Stop the whole restore. A difference in a block whose meaning is
            # contradicted is not something to write through -- it is
            # something nobody predicted, and the right response is a person.
            results.append(
                ChannelRestore(
                    output=ch,
                    differing_blocks=tuple(differing),
                    written_blocks=(),
                    verified=False,
                    blocked=blocked,
                )
            )
            raise RestoreBlocked(
                f"output {ch + 1} differs from the snapshot at block(s) "
                f"{list(blocked)}, whose meaning is contradicted between the "
                f"decompiled app and the device's own readback. Nothing has "
                f"been written. Escalate: this difference was not predicted."
            )

        if dry_run or not differing:
            results.append(
                ChannelRestore(
                    output=ch,
                    differing_blocks=tuple(differing),
                    written_blocks=(),
                    verified=not differing,
                )
            )
            continue

        written = device.apply_record(ch, target, reason=reason)
        verified = device.refresh(ch) == target
        results.append(
            ChannelRestore(
                output=ch,
                differing_blocks=tuple(differing),
                written_blocks=tuple(written),
                verified=verified,
            )
        )
        if not verified:
            raise SnapshotError(
                f"output {ch + 1} does not match the snapshot after restore. "
                f"Stopping with {len(targets) - targets.index(ch) - 1} "
                f"channel(s) untouched."
            )

    return RestoreReport(channels=tuple(results), dry_run=dry_run)


@dataclass(frozen=True)
class PresetRestoreReport:
    """What a preset-based rollback did, and whether it worked.

    Deliberately shaped like :class:`RestoreReport` in the one way that
    matters -- :attr:`device_matches` is the only thing a caller should act on,
    and it is set by re-reading the device, never by the operation reporting
    success.
    """

    slot: int
    #: Blocks still differing from the target, per channel. Empty means clean.
    differing: dict[int, list[int]]
    dry_run: bool

    @property
    def device_matches(self) -> bool:
        return not self.differing

    def summary(self) -> str:
        what = "would recall" if self.dry_run else "recalled"
        if self.device_matches:
            return f"{what} preset {self.slot}; device matches, 0 blocks differ"
        n = sum(len(v) for v in self.differing.values())
        return (
            f"{what} preset {self.slot}; **{n} block(s) still differ** across "
            f"channels {sorted(self.differing)}"
        )


def store_as_preset(
    device: Dsp408Device,
    snapshot: DeviceSnapshot,
    slot: int,
    name: str,
) -> None:
    """Write a snapshot into a preset slot.

    The safe half of the preset pair: storage is written, no audio changes, and
    the working area is untouched. Doing this at the start of a tuning run buys
    a rollback that survives our process dying, the host going away, and the
    file being lost -- which none of the other restore paths do.

    The snapshot's records are written verbatim, including blocks 34 and 35.
    That is correct here and would not be for :func:`restore`: those blocks are
    refused for *working-area* writes because their meaning is contradicted, but
    a preset store round-trips a record the device itself produced, so declining
    to carry two of its blocks would store a tune that is not the one captured.
    """
    device.session.store_preset(slot, name, snapshot.channels)


def restore_from_preset(
    device: Dsp408Device,
    slot: int,
    expect: DeviceSnapshot | None = None,
    dry_run: bool = True,
) -> PresetRestoreReport:
    """Roll the whole device back by recalling a preset.

    One transaction sequence replaces all eight channels in about five seconds,
    against :func:`restore`'s block-by-block write-and-verify. Use this when the
    baseline was stored to a slot; use :func:`restore` when it was not.

    ``dry_run`` defaults to True and, unlike the block-level restore, a dry run
    here can only *report what it would do* -- there is no way to ask the device
    what a slot contains without loading it, because the read is the recall.
    That asymmetry is the device's, not ours.

    If ``expect`` is given, the working area is re-read afterwards and compared
    against it block by block. **That comparison is the only evidence the
    rollback worked**; the recall returning eight records is not, because the
    device answers with the slot's contents whether or not it applied them.
    """
    if dry_run:
        return PresetRestoreReport(slot=slot, differing={}, dry_run=True)

    device.session.recall_preset(slot)
    # Every cached record is now stale -- the recall replaced all eight at
    # once. refresh() re-reads and re-caches, so refreshing them all is both
    # the invalidation and the evidence.
    live = device.refresh_all()

    differing: dict[int, list[int]] = {}
    if expect is not None:
        for ch in range(N_OUTPUTS):
            blocks = diff_blocks(live[ch], expect.channels[ch])
            if blocks:
                differing[ch] = blocks
    return PresetRestoreReport(slot=slot, differing=differing, dry_run=False)
