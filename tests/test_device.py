"""Read-modify-write, arming, verification and the journal.

These are the guards standing between a bug and an irreplaceable DSP, so they
are tested for what they *refuse* at least as hard as for what they permit.
"""

from __future__ import annotations

import json

import pytest

from tuner.dsp.device import (
    BLOCK_LEN,
    DeviceError,
    Dsp408Device,
    JournalEntry,
    ReadbackMismatch,
    SnapshotEvidence,
    UnsendablePlan,
    UnverifiedBlock,
    WriteJournal,
    WriteOutcome,
    WritesNotArmed,
    describe_diff,
    diff_blocks,
)
from tuner.dsp.fake_device import FakeDsp408
from tuner.dsp.protocol import UnsendableFrame
from tuner.dsp.session import Dsp408Session, Pacing
from tuner.dsp.transport import LoopbackTransport
from tuner.dsp.txpolicy import BlastRadius, TxPolicy

GAIN_BLOCK = 31


@pytest.fixture
def rig(tmp_path):
    fake = FakeDsp408()
    session = Dsp408Session(
        LoopbackTransport(fake),
        policy=TxPolicy(
            allow_writes=True,
            blast_radius=BlastRadius(max_writes=999, max_channels=8),
        ),
        # No pacing delays: the timing policy is tested in test_session.py,
        # and a real 0.1 s per request would make this suite take minutes.
        pacing=Pacing(idle_after_reply_s=0.0, max_requests_per_s=1e9),
    )
    session.open()
    device = Dsp408Device(
        session,
        journal=WriteJournal(tmp_path / "journal.jsonl"),
        session_id="test",
        clock=lambda: 1_700_000_000.0,
    )
    return device, fake, tmp_path


@pytest.fixture
def armed(rig):
    device, fake, tmp_path = rig
    evidence = _evidence(device, tmp_path)
    device.arm_writes("tests", evidence)
    return device, fake, tmp_path


def _evidence(device, tmp_path, name="snap.bin"):
    """Minimal proof-of-restore-point, without involving snapshot.py."""
    import hashlib

    path = tmp_path / name
    path.write_bytes(b"".join(device.refresh_all()))
    return SnapshotEvidence(
        path=path,
        digest=hashlib.sha256(path.read_bytes()).hexdigest(),
        firmware="MYDW-AV1.06",
        session_id="test",
    )


def _set_gain(raw: int):
    return lambda b: bytes(b[:2]) + raw.to_bytes(2, "little") + bytes(b[4:])


class TestArming:
    def test_writes_are_refused_unarmed(self, rig):
        device, fake, _ = rig
        with pytest.raises(WritesNotArmed, match="no undo"):
            device.write_block(0, GAIN_BLOCK, bytes(8))
        assert fake.writes == []

    def test_arming_needs_a_reason(self, rig):
        device, _, tmp_path = rig
        with pytest.raises(WritesNotArmed, match="reason"):
            device.arm_writes("   ", _evidence(device, tmp_path))

    def test_arming_needs_a_snapshot_that_exists(self, rig):
        device, _, tmp_path = rig
        evidence = _evidence(device, tmp_path)
        evidence.path.unlink()
        with pytest.raises(WritesNotArmed, match="is gone"):
            device.arm_writes("tests", evidence)

    def test_arming_needs_a_snapshot_that_still_matches(self, rig):
        device, _, tmp_path = rig
        evidence = _evidence(device, tmp_path)
        evidence.path.write_bytes(b"something else")
        with pytest.raises(WritesNotArmed, match="has changed"):
            device.arm_writes("tests", evidence)

    def test_a_snapshot_deleted_mid_run_stops_further_writes(self, armed):
        # Re-verified at every write, not only at arming. A restore point that
        # vanishes halfway through is exactly when it matters.
        device, fake, _ = armed
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        device._evidence.path.unlink()
        with pytest.raises(WritesNotArmed, match="is gone"):
            device.modify_block(0, GAIN_BLOCK, _set_gain(480))

    def test_disarm_stops_writes(self, armed):
        device, _, _ = armed
        device.disarm()
        assert not device.armed
        with pytest.raises(WritesNotArmed):
            device.write_block(0, GAIN_BLOCK, bytes(8))


class TestReadModifyWrite:
    def test_a_modify_preserves_every_other_field(self, armed):
        # The bug this whole module exists to prevent: the app's gain writes
        # also carried mute, polarity, delay, eq_mode and spk_type.
        device, fake, _ = armed
        before = device.block(0, GAIN_BLOCK)
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        after = fake.image.channels[0][
            GAIN_BLOCK * BLOCK_LEN : (GAIN_BLOCK + 1) * BLOCK_LEN
        ]
        assert int.from_bytes(after[2:4], "little") == 490
        assert after[:2] == before[:2]  # mute, polarity
        assert after[4:] == before[4:]  # delay, eq_mode, spk_type

    def test_a_no_op_write_does_not_transmit(self, armed):
        # Fewest writes is fewest risks, and it makes restore idempotent.
        device, fake, _ = armed
        same = device.block(0, GAIN_BLOCK)
        assert device.write_block(0, GAIN_BLOCK, same) is False
        assert fake.writes == []
        assert device.stats.blocks_skipped == 1

    def test_a_real_change_transmits_once(self, armed):
        device, fake, _ = armed
        assert device.modify_block(0, GAIN_BLOCK, _set_gain(490)) is True
        assert len(fake.writes) == 1

    def test_the_cache_is_refreshed_by_verification(self, armed):
        device, _, _ = armed
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        assert int.from_bytes(device.block(0, GAIN_BLOCK)[2:4], "little") == 490

    def test_a_mutate_returning_the_wrong_length_is_rejected(self, armed):
        device, fake, _ = armed
        with pytest.raises(DeviceError, match="a block is 8"):
            device.modify_block(0, GAIN_BLOCK, lambda b: b[:4])
        assert fake.writes == []

    @pytest.mark.parametrize("data_id", [34, 35])
    def test_the_contradicted_blocks_are_refused(self, armed, data_id):
        device, fake, _ = armed
        with pytest.raises(UnverifiedBlock, match="contradicted"):
            device.write_block(0, data_id, bytes(8))
        assert fake.writes == []

    @pytest.mark.parametrize("output", [-1, 8, 99])
    def test_bad_outputs_are_refused(self, armed, output):
        device, _, _ = armed
        with pytest.raises(DeviceError, match="outside"):
            device.write_block(output, GAIN_BLOCK, bytes(8))

    @pytest.mark.parametrize("data_id", [-1, 37, 119])
    def test_bad_block_ids_are_refused(self, armed, data_id):
        device, _, _ = armed
        with pytest.raises(DeviceError, match="outside"):
            device.write_block(0, data_id, bytes(8))


class TestVerification:
    def test_a_lying_device_is_caught(self, armed):
        # The device acks the write but does not apply it.
        device, fake, _ = armed
        original = fake._write

        def swallow(frame):
            original(frame)
            fake.image.channels[0][248:256] = bytes(8)

        fake._write = swallow
        with pytest.raises(ReadbackMismatch, match="whole 296-byte record"):
            device.modify_block(0, GAIN_BLOCK, _set_gain(490))

    def test_an_off_by_one_data_id_is_caught(self, armed):
        # The failure a per-block check cannot see: the right bytes land eight
        # bytes away, so the targeted block looks untouched and its neighbour
        # is quietly wrong.
        device, fake, _ = armed

        def shifted(frame):
            did = int(frame.data_id) + 1
            fake.image.channels[int(frame.channel_id)][
                did * BLOCK_LEN : (did + 1) * BLOCK_LEN
            ] = frame.payload

        fake._write = shifted
        with pytest.raises(ReadbackMismatch):
            device.modify_block(0, GAIN_BLOCK, _set_gain(490))

    def test_verification_reads_the_whole_record(self, armed):
        device, _, _ = armed
        before = device.stats.reads
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        assert device.stats.reads > before
        assert device.stats.verifications == 1


class TestJournal:
    def test_an_entry_is_written_before_the_frame_goes_out(self, armed):
        # Ordering is the whole point. A journal written after a successful
        # write records only the case that needed no record.
        device, fake, tmp_path = armed
        path = tmp_path / "journal.jsonl"
        seen = {}
        original = fake._write

        def capture_state(frame):
            seen["journal_lines"] = (
                len(path.read_text().splitlines()) if path.exists() else 0
            )
            original(frame)

        fake._write = capture_state
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        assert seen["journal_lines"] == 1

    def test_it_records_before_and_after(self, armed):
        device, _, tmp_path = armed
        before = device.block(0, GAIN_BLOCK)
        device.modify_block(0, GAIN_BLOCK, _set_gain(490), reason="stage 5")
        (line,) = (tmp_path / "journal.jsonl").read_text().splitlines()
        entry = json.loads(line)
        assert entry["before"] == before.hex()
        assert int.from_bytes(bytes.fromhex(entry["after"])[2:4], "little") == 490
        assert entry["output"] == 0
        assert entry["data_id"] == GAIN_BLOCK
        assert entry["reason"] == "stage 5"
        assert entry["session_id"] == "test"

    def test_a_skipped_write_is_not_journalled(self, armed):
        device, _, tmp_path = armed
        device.write_block(0, GAIN_BLOCK, device.block(0, GAIN_BLOCK))
        assert not (tmp_path / "journal.jsonl").exists()

    def test_it_round_trips_through_disk(self, armed):
        device, _, tmp_path = armed
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        device.modify_block(1, GAIN_BLOCK, _set_gain(480))
        loaded = WriteJournal.load(tmp_path / "journal.jsonl")
        assert len(loaded.entries) == 2
        assert [e.output for e in loaded.entries] == [0, 1]

    def test_a_journal_without_a_path_still_records_in_memory(self, rig, tmp_path):
        device, _, _ = rig
        device.journal = WriteJournal(None)
        device.arm_writes("tests", _evidence(device, tmp_path))
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        assert len(device.journal.entries) == 1

    def test_entries_serialise_stably(self):
        entry = JournalEntry(
            timestamp=1.0,
            session_id="s",
            output=0,
            data_id=31,
            before="00",
            after="01",
            reason="r",
        )
        assert JournalEntry.from_json(entry.as_json()) == entry


class TestReconcile:
    def test_a_landed_write_is_recognised(self, armed):
        device, _, _ = armed
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        (result,) = device.reconcile()
        assert result.outcome is WriteOutcome.LANDED
        assert not result.needs_attention

    def test_a_write_that_never_reached_the_device_is_recognised(self, armed):
        # Simulates the crash case: journalled, then nothing happened.
        device, _, _ = armed
        before = device.block(0, GAIN_BLOCK)
        device.journal.append(
            JournalEntry(
                timestamp=0.0,
                session_id="test",
                output=0,
                data_id=GAIN_BLOCK,
                before=before.hex(),
                after=bytes(8).hex(),
                reason="never sent",
            )
        )
        (result,) = device.reconcile()
        assert result.outcome is WriteOutcome.NOT_LANDED
        assert result.needs_attention

    def test_an_unexpected_value_is_conflicting(self, armed):
        device, fake, _ = armed
        before = device.block(0, GAIN_BLOCK)
        device.journal.append(
            JournalEntry(
                timestamp=0.0,
                session_id="test",
                output=0,
                data_id=GAIN_BLOCK,
                before=before.hex(),
                after=bytes(8).hex(),
                reason="x",
            )
        )
        fake.image.channels[0][248:256] = bytes([0xAB] * 8)
        (result,) = device.reconcile()
        assert result.outcome is WriteOutcome.CONFLICTING
        assert result.actual == bytes([0xAB] * 8).hex()

    def test_a_rollback_reports_its_own_writes_as_not_landed(self, armed):
        # Documented behaviour, not a bug: reconcile answers "does the device
        # hold this now", and after a deliberate rollback it does not.
        device, _, _ = armed
        original = device.block(0, GAIN_BLOCK)
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        device.write_block(0, GAIN_BLOCK, original)
        outcomes = [r.outcome for r in device.reconcile()]
        assert outcomes == [WriteOutcome.NOT_LANDED, WriteOutcome.LANDED]

    def test_it_reads_each_touched_channel_once(self, armed):
        device, _, _ = armed
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        device.modify_block(0, 32, lambda b: bytes([9]) + bytes(b[1:]))
        before = device.stats.reads
        device.reconcile()
        assert device.stats.reads - before == 1  # one channel touched


class TestApplyRecord:
    def test_only_differing_blocks_are_written(self, armed):
        device, fake, _ = armed
        target = bytearray(device.refresh(0))
        target[248:250] = bytes([0, 0])
        target[0:8] = bytes([1, 2, 3, 4, 5, 6, 7, 8])
        written = device.apply_record(0, bytes(target))
        assert sorted(written) == [0, GAIN_BLOCK]
        assert len(fake.writes) == 2

    def test_an_identical_record_writes_nothing(self, armed):
        device, fake, _ = armed
        assert device.apply_record(0, device.refresh(0)) == []
        assert fake.writes == []

    def test_a_contradicted_block_blocks_the_whole_record(self, armed):
        # Refused before anything is transmitted, so a restore never
        # half-completes into a state nobody planned.
        device, fake, _ = armed
        target = bytearray(device.refresh(0))
        target[0:8] = bytes(8)  # a legitimate change
        target[34 * 8 : 35 * 8] = bytes([0xFF] * 8)  # and a forbidden one
        with pytest.raises(UnverifiedBlock, match="Escalate"):
            device.apply_record(0, bytes(target))
        assert fake.writes == []

    def test_a_wrong_length_record_is_refused(self, armed):
        device, _, _ = armed
        with pytest.raises(DeviceError, match="expected 296"):
            device.apply_record(0, bytes(100))


class TestDiffHelpers:
    def test_diff_blocks_finds_the_right_indices(self):
        a = bytes(296)
        b = bytearray(296)
        b[8:16] = bytes([1] * 8)
        b[248:256] = bytes([2] * 8)
        assert diff_blocks(a, bytes(b)) == [1, 31]

    def test_diff_blocks_rejects_mismatched_lengths(self):
        with pytest.raises(DeviceError, match="differ in length"):
            diff_blocks(bytes(8), bytes(16))

    def test_describe_diff_names_the_blocks(self):
        a = bytes(296)
        b = bytearray(296)
        b[248:256] = bytes([2] * 8)
        b[8:16] = bytes([1] * 8)
        lines = describe_diff(a, bytes(b))
        assert "EQ band 1" in lines[0]
        assert "MISC" in lines[1]


def _gain(fake, ch):
    return int.from_bytes(fake.image.channels[ch][31 * 8 + 2 : 31 * 8 + 4], "little")


class TestLinkedChannels:
    """Mirroring across a link group.

    Measured 2026-08-09 from ``captures/btsnoop_hci_2026-08-09_link.log``.
    Three gain steps on a linked output produced six writes -- both channels,
    ~10 ms apart, identical gains; the same three steps unlinked produced
    three, on one channel. **The app mirrors, the device does not.**
    """

    @staticmethod
    def _link(fake, channels, group=1):
        for ch in range(8):
            blk = bytearray(fake.image.channels[ch][35 * 8 : 36 * 8])
            blk[7] = group if ch in channels else 0
            fake.image.channels[ch][35 * 8 : 36 * 8] = bytes(blk)

    def test_partners_are_read_from_the_device(self, armed):
        device, fake, _ = armed
        self._link(fake, {6, 7})
        device.refresh_all()
        assert device.link_partners(6) == (7,)
        assert device.link_partners(7) == (6,)
        assert device.link_partners(0) == ()

    def test_group_zero_means_no_group(self, armed):
        # Every unlinked channel stores 0. Treating that as a group would gang
        # all six of them together.
        device, fake, _ = armed
        self._link(fake, set())
        device.refresh_all()
        for ch in range(8):
            assert device.link_partners(ch) == ()

    def test_a_plain_write_touches_only_one_channel(self, armed):
        # The measured device behaviour, and the reason mirroring has to be
        # done by us rather than assumed.
        device, fake, _ = armed
        self._link(fake, {6, 7})
        device.refresh_all()
        device.modify_block(6, GAIN_BLOCK, _set_gain(400))
        assert _gain(fake, 6) == 400
        assert _gain(fake, 7) != 400

    def test_a_mirrored_write_touches_the_pair(self, armed):
        device, fake, _ = armed
        self._link(fake, {6, 7})
        device.refresh_all()
        touched = device.modify_block_mirrored(6, GAIN_BLOCK, _set_gain(400))
        assert set(touched) == {6, 7}
        assert _gain(fake, 6) == _gain(fake, 7) == 400

    def test_mirroring_preserves_each_channels_own_bytes(self, armed):
        # The partner is read-modify-written, not sent a copy of the first
        # channel's block. spk_type differs across the real linked pair (15 and
        # 18), and broadcasting one payload would silently overwrite it.
        device, fake, _ = armed
        self._link(fake, {6, 7})
        for ch, spk in ((6, 15), (7, 18)):
            blk = bytearray(fake.image.channels[ch][31 * 8 : 32 * 8])
            blk[7] = spk
            fake.image.channels[ch][31 * 8 : 32 * 8] = bytes(blk)
        device.refresh_all()

        device.modify_block_mirrored(6, GAIN_BLOCK, _set_gain(400))
        assert fake.image.channels[6][31 * 8 + 7] == 15
        assert fake.image.channels[7][31 * 8 + 7] == 18

    def test_mirroring_an_unlinked_channel_is_just_a_write(self, armed):
        device, fake, _ = armed
        self._link(fake, set())
        device.refresh_all()
        assert device.modify_block_mirrored(0, GAIN_BLOCK, _set_gain(400)) == (0,)


class TestNoOpWrite:
    """Bring-up Stage 4: the first write, shaped so it cannot change anything.

    ``write_block`` deliberately declines to transmit a payload the device
    already holds -- right for a restore, and it made Stage 4 unreachable
    through the sanctioned write path. ``rewrite_block_unchanged`` is that
    path.
    """

    def test_it_transmits_where_write_block_would_not(self, armed):
        device, fake, _ = armed
        same = device.block(0, GAIN_BLOCK)
        assert device.write_block(0, GAIN_BLOCK, same) is False
        before = device.stats.writes
        device.rewrite_block_unchanged(0, GAIN_BLOCK)
        assert device.stats.writes == before + 1

    def test_it_cannot_change_the_record(self, armed):
        device, _, _ = armed
        before = device.record(0)
        device.rewrite_block_unchanged(0, GAIN_BLOCK)
        assert device.refresh(0) == before

    def test_it_sends_the_live_bytes_not_the_cached_ones(self, armed):
        # The whole safety property. If someone changed the channel from the
        # vendor app while we held a stale cache, a cached payload would be a
        # real write reverting their change -- which is exactly what Stage 4
        # must be incapable of.
        device, fake, _ = armed
        device.record(0)  # prime the cache
        fake.image.channels[0][
            GAIN_BLOCK * BLOCK_LEN + 2 : GAIN_BLOCK * BLOCK_LEN + 4
        ] = (444).to_bytes(2, "little")
        sent = device.rewrite_block_unchanged(0, GAIN_BLOCK)
        assert int.from_bytes(sent[2:4], "little") == 444
        assert (
            device.refresh(0)[GAIN_BLOCK * BLOCK_LEN : (GAIN_BLOCK + 1) * BLOCK_LEN]
            == sent
        )

    def test_the_journal_entry_has_before_equal_to_after(self, armed):
        device, _, _ = armed
        sent = device.rewrite_block_unchanged(0, GAIN_BLOCK, reason="stage 4")
        entry = device.journal.entries[-1]
        assert entry.before == entry.after == sent.hex()
        assert entry.reason == "stage 4"

    def test_reconcile_can_never_call_a_no_op_not_landed(self, armed):
        # Documented limitation, pinned so it is a known property rather than
        # a surprise: for a no-op, landed and not-landed are the same readback.
        device, _, _ = armed
        device.rewrite_block_unchanged(0, GAIN_BLOCK)
        results = device.reconcile()
        assert [r.outcome for r in results] == [WriteOutcome.LANDED]

    def test_it_reports_conflict_if_the_device_moved_underneath(self, armed):
        device, fake, _ = armed
        device.rewrite_block_unchanged(0, GAIN_BLOCK)
        fake.image.channels[0][
            GAIN_BLOCK * BLOCK_LEN + 2 : GAIN_BLOCK * BLOCK_LEN + 4
        ] = (321).to_bytes(2, "little")
        results = device.reconcile()
        assert [r.outcome for r in results] == [WriteOutcome.CONFLICTING]

    def test_it_refuses_unarmed(self, rig):
        device, _, _ = rig
        with pytest.raises(WritesNotArmed):
            device.rewrite_block_unchanged(0, GAIN_BLOCK)

    @pytest.mark.parametrize("block", [34, 35])
    def test_it_refuses_a_contradicted_block(self, armed, block):
        # Not even unchanged. A block whose meaning is contradicted between
        # the decompiled app and the device's own readback is one we cannot
        # predict the effect of writing, and "the bytes are the same" is a
        # claim about our decode, which is the thing in doubt.
        device, _, _ = armed
        with pytest.raises(UnverifiedBlock):
            device.rewrite_block_unchanged(0, block)

    def test_it_refuses_an_out_of_range_output(self, armed):
        device, _, _ = armed
        with pytest.raises(DeviceError):
            device.rewrite_block_unchanged(8, GAIN_BLOCK)

    def test_a_swallowed_no_op_is_still_invisible(self, armed):
        # Stage 4's honest limit, asserted rather than only documented: a
        # device that discards the write passes every check here. Only Stage 5
        # can tell the difference, which is why Stage 5 is a separate rung.
        device, fake, _ = armed
        device.record(0)  # prime the cache so the swallow cannot break the read

        swallowed = []

        def swallow(frame):
            swallowed.append(frame)
            return None

        fake._write = swallow
        device.rewrite_block_unchanged(0, GAIN_BLOCK)  # no ReadbackMismatch
        assert len(swallowed) == 1
        assert device.stats.writes == 1


class TestGangWriteIsAtomic:
    """A mirrored write reaches every member of the pair, or none of them.

    ``channel_id`` is inside the frame checksum, so the same parameter value
    can be sendable on one member of a link group and unsendable on the other.
    Writing members in sequence then leaves the pair **mismatched** -- and for
    outputs 7 and 8, two subwoofers sharing a ported box, an unequal pair is
    the mechanical-failure case that the gang rules exist to prevent.

    Measured 2026-08-11 on the operator's device: four ``gain_raw`` values out
    of 601 (225, 253, 480, 508) are writable on exactly one member of that
    pair. Rare enough never to appear by accident in testing, common enough
    for a fit to land on.
    """

    #: One of the four, from that scan. gain_raw 480 = -12.0 dB.
    SPLIT_GAIN_RAW = 480

    @staticmethod
    def _link(fake, a, b, group=1):
        fake.image.channels[a][280 + 7] = group
        fake.image.channels[b][280 + 7] = group

    def test_both_members_are_written(self, armed):
        device, fake, _ = armed
        self._link(fake, 6, 7)
        device.refresh_all()
        device.session.policy.acknowledge_gang({6, 7})
        touched = device.modify_block_mirrored(6, GAIN_BLOCK, _set_gain(430))
        assert sorted(touched) == [6, 7]
        for ch in (6, 7):
            block = fake.image.channels[ch][
                GAIN_BLOCK * BLOCK_LEN : (GAIN_BLOCK + 1) * BLOCK_LEN
            ]
            assert int.from_bytes(block[2:4], "little") == 430

    def test_an_unsendable_member_stops_the_whole_write(self, armed):
        # The regression test. Before the pre-flight, the first member was
        # written and the second raised, leaving the pair unequal.
        device, fake, _ = armed
        self._link(fake, 6, 7)
        device.refresh_all()
        device.session.policy.acknowledge_gang({6, 7})

        before = {
            ch: bytes(
                fake.image.channels[ch][
                    GAIN_BLOCK * BLOCK_LEN : (GAIN_BLOCK + 1) * BLOCK_LEN
                ]
            )
            for ch in (6, 7)
        }

        # Make member 7 refuse to encode, leaving member 6 perfectly sendable.
        real = device.session.block_write_frame

        def picky(output, data_id, payload):
            frame = real(output, data_id, payload)
            if output == 7:
                raise UnsendableFrame("synthetic zero checksum on the partner")
            return frame

        device.session.block_write_frame = picky
        with pytest.raises(UnsendablePlan):
            device.modify_block_mirrored(6, GAIN_BLOCK, _set_gain(430))

        assert fake.writes == []
        for ch in (6, 7):
            assert (
                bytes(
                    fake.image.channels[ch][
                        GAIN_BLOCK * BLOCK_LEN : (GAIN_BLOCK + 1) * BLOCK_LEN
                    ]
                )
                == before[ch]
            )

    def test_the_pair_still_matches_after_a_refusal(self, armed):
        # Stated as the property that actually matters, rather than as "no
        # bytes moved": the invariant is that the two subwoofers agree.
        device, fake, _ = armed
        self._link(fake, 6, 7)
        device.refresh_all()
        device.session.policy.acknowledge_gang({6, 7})
        real = device.session.block_write_frame

        def picky(output, data_id, payload):
            if output == 7:
                raise UnsendableFrame("synthetic")
            return real(output, data_id, payload)

        device.session.block_write_frame = picky
        with pytest.raises(UnsendablePlan):
            device.modify_block_mirrored(6, GAIN_BLOCK, _set_gain(430))

        gains = [
            int.from_bytes(
                fake.image.channels[ch][
                    GAIN_BLOCK * BLOCK_LEN + 2 : GAIN_BLOCK * BLOCK_LEN + 4
                ],
                "little",
            )
            for ch in (6, 7)
        ]
        assert gains[0] == gains[1]

    def test_a_bad_mutate_is_caught_before_any_member_is_written(self, armed):
        device, fake, _ = armed
        self._link(fake, 6, 7)
        device.refresh_all()
        device.session.policy.acknowledge_gang({6, 7})
        with pytest.raises(DeviceError, match="a block is 8"):
            device.modify_block_mirrored(6, GAIN_BLOCK, lambda b: b[:4])
        assert fake.writes == []

    def test_each_member_keeps_its_own_undecoded_bytes(self, armed):
        # Why the mutate runs per channel instead of one payload being
        # broadcast: spk_type differs across a linked pair.
        device, fake, _ = armed
        self._link(fake, 6, 7)
        fake.image.channels[6][GAIN_BLOCK * BLOCK_LEN + 7] = 3
        fake.image.channels[7][GAIN_BLOCK * BLOCK_LEN + 7] = 5
        device.refresh_all()
        device.session.policy.acknowledge_gang({6, 7})
        device.modify_block_mirrored(6, GAIN_BLOCK, _set_gain(430))
        assert fake.image.channels[6][GAIN_BLOCK * BLOCK_LEN + 7] == 3
        assert fake.image.channels[7][GAIN_BLOCK * BLOCK_LEN + 7] == 5
