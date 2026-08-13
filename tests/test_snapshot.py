"""Snapshot and restore -- the mechanism behind the rollback clause.

An adversarial review's sharpest finding was that the improvement invariant
demanded "automatic rollback verified by re-measurement" while nothing in the
codebase could restore a device. These tests are what makes that a mechanism
instead of a policy.

They are written around the ways a restore can be *quietly* wrong: a snapshot
that decoded what it could not represent, a verification that checked too
narrow a window, a restore that half-completed, or a green report on a device
that does not actually match.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tuner.dsp import ddp
from tuner.dsp import snapshot as snap
from tuner.dsp.device import (
    Dsp408Device,
    ReadbackMismatch,
    WriteJournal,
    WritesNotArmed,
)
from tuner.dsp.fake_device import DeviceImage, FakeDsp408
from tuner.dsp.protocol import ProtocolError
from tuner.dsp.session import Dsp408Session, Pacing
from tuner.dsp.transport import LoopbackTransport
from tuner.dsp.txpolicy import BlastRadius, TxPolicy, TxRefused

REPO = Path(__file__).resolve().parents[1]
GAIN_BLOCK = 31


def _rig(tmp_path, image=None):
    fake = FakeDsp408(image or DeviceImage.flat())
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
    identity = session.handshake()
    device = Dsp408Device(
        session, journal=WriteJournal(tmp_path / "j.jsonl"), session_id="test"
    )
    return device, fake, identity


@pytest.fixture
def rig(tmp_path):
    return _rig(tmp_path)


@pytest.fixture
def armed(tmp_path, rig):
    device, fake, identity = rig
    shot = snap.capture(device, identity, transport_name="loopback", now=0.0)
    device.arm_writes("tests", shot.save(tmp_path / "baseline.json"))
    return device, fake, shot


def _set_gain(raw: int):
    return lambda b: bytes(b[:2]) + raw.to_bytes(2, "little") + bytes(b[4:])


class TestCapture:
    def test_it_records_every_channel_verbatim(self, rig):
        device, fake, identity = rig
        shot = snap.capture(device, identity, now=0.0)
        assert len(shot.channels) == 8
        assert all(len(r) == 296 for r in shot.channels)
        for ch in range(8):
            assert shot.channels[ch] == bytes(fake.image.channels[ch])

    def test_it_stores_bytes_not_decoded_fields(self, rig):
        # The load-bearing design decision. Blocks 34/35 are undecoded and
        # several encodings are unknown; a snapshot built from parsed fields
        # would lose or re-guess them.
        device, fake, identity = rig
        fake.image.channels[3][34 * 8 : 35 * 8] = bytes([0xDE, 0xAD] * 4)
        shot = snap.capture(device, identity, now=0.0)
        assert shot.block(3, 34) == bytes([0xDE, 0xAD] * 4)

    def test_it_records_provenance_and_the_system_blocks(self, rig):
        device, _, identity = rig
        shot = snap.capture(
            device, identity, transport_name="loopback", notes={"why": "test"}, now=0.0
        )
        assert shot.provenance.firmware == "MYDW-AV1.06"
        assert shot.provenance.transport == "loopback"
        assert shot.provenance.session_id == "test"
        assert shot.provenance.captured_utc.endswith("Z")
        assert shot.provenance.notes == {"why": "test"}
        assert shot.system_blocks[4] == b"MYDW-AV1.06"
        assert shot.current_preset == 4
        assert len(shot.preset_names) == 15

    def test_the_digest_covers_state_but_not_provenance(self, rig):
        # Two snapshots of the same device state must compare equal even if
        # taken a minute apart.
        device, _, identity = rig
        a = snap.capture(device, identity, now=0.0)
        b = snap.capture(device, identity, now=99_999.0)
        assert a.provenance.captured_utc != b.provenance.captured_utc
        assert a.digest == b.digest

    def test_a_changed_device_changes_the_digest(self, rig):
        device, fake, identity = rig
        a = snap.capture(device, identity, now=0.0)
        fake.image.channels[0][250] ^= 0xFF
        b = snap.capture(device, identity, now=0.0)
        assert a.digest != b.digest


class TestPersistence:
    def test_it_round_trips_through_disk(self, tmp_path, rig):
        device, _, identity = rig
        shot = snap.capture(device, identity, now=0.0)
        shot.save(tmp_path / "s.json")
        loaded = snap.DeviceSnapshot.load(tmp_path / "s.json")
        assert loaded.channels == shot.channels
        assert loaded.digest == shot.digest
        assert loaded.system_blocks == shot.system_blocks
        assert loaded.preset_names == shot.preset_names

    def test_saving_returns_evidence_that_arms_writes(self, tmp_path, rig):
        device, _, identity = rig
        shot = snap.capture(device, identity, now=0.0)
        evidence = shot.save(tmp_path / "s.json")
        device.arm_writes("because", evidence)
        assert device.armed

    def test_a_tampered_file_is_refused(self, tmp_path, rig):
        device, _, identity = rig
        shot = snap.capture(device, identity, now=0.0)
        path = tmp_path / "s.json"
        shot.save(path)

        body = json.loads(path.read_text())
        body["channels"][0] = "ff" * 296
        path.write_text(json.dumps(body))

        with pytest.raises(snap.SnapshotCorrupt, match="digest mismatch"):
            snap.DeviceSnapshot.load(path)

    def test_an_unknown_schema_is_refused(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"schema": "something/9"}))
        with pytest.raises(snap.SnapshotCorrupt, match="schema"):
            snap.DeviceSnapshot.load(path)

    def test_no_temp_file_is_left_behind(self, tmp_path, rig):
        device, _, identity = rig
        snap.capture(device, identity, now=0.0).save(tmp_path / "s.json")
        assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []

    def test_a_wrong_channel_count_is_rejected(self):
        with pytest.raises(snap.SnapshotError, match="expected 8"):
            snap.DeviceSnapshot(
                channels=(bytes(296),),
                system_blocks={},
                preset_names=(),
                current_preset=1,
                provenance=snap.SnapshotProvenance("", "", "", ""),
            )

    def test_a_wrong_record_length_is_rejected(self):
        with pytest.raises(snap.SnapshotError, match="expected 296"):
            snap.DeviceSnapshot(
                channels=tuple([bytes(296)] * 7 + [bytes(100)]),
                system_blocks={},
                preset_names=(),
                current_preset=1,
                provenance=snap.SnapshotProvenance("", "", "", ""),
            )


class TestCompare:
    def test_no_drift_reports_nothing(self, armed):
        device, _, shot = armed
        assert snap.compare(device, shot) == {}

    def test_it_finds_a_change_without_writing(self, armed):
        device, fake, shot = armed
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        assert snap.compare(device, shot) == {0: [GAIN_BLOCK]}

    def test_it_detects_a_preset_recall_behind_our_back(self, armed):
        # The scenario that makes a comparison worth running at all: something
        # changed the device and it was not us. Preset recall is the one
        # action that destroys the working area, so it is the realistic way
        # for a device to stop matching a snapshot without any write from us.
        device, fake, shot = armed

        # Park a *different* tune in slot 5, then put the device back so it
        # genuinely matches the snapshot again.
        #
        # This used slot 9 until 2026-08-09, when a capture showed there are
        # six slots and not fifteen -- 7-15 answer a name read from a stale
        # buffer and store nothing. The test still passed with 9 because the
        # fake modelled fifteen too; both were wrong together, which is the
        # failure mode a fake has that a real device does not.
        device.modify_block(0, GAIN_BLOCK, _set_gain(400))
        fake.store_preset(5)
        snap.restore(device, shot, dry_run=False)
        assert snap.compare(device, shot) == {}

        # Now someone recalls slot 5. No write of ours is involved.
        fake.recall_preset(5)
        assert snap.compare(device, shot) == {0: [GAIN_BLOCK]}


class TestRestore:
    def test_a_dry_run_writes_nothing(self, armed):
        device, fake, shot = armed
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        fake.writes.clear()

        report = snap.restore(device, shot, dry_run=True)
        assert fake.writes == []
        assert report.total_writes == 0
        assert report.blocks_to_write == 1
        assert not report.device_matches
        assert "would write" in report.summary()

    def test_dry_run_is_the_default(self, armed):
        device, fake, shot = armed
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        fake.writes.clear()
        snap.restore(device, shot)
        assert fake.writes == []

    def test_it_puts_the_device_back(self, armed):
        device, fake, shot = armed
        before = bytes(fake.image.channels[0])
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        assert bytes(fake.image.channels[0]) != before

        report = snap.restore(device, shot, dry_run=False)
        assert report.clean
        assert report.total_writes == 1
        assert bytes(fake.image.channels[0]) == before

    def test_it_writes_only_what_differs(self, armed):
        device, fake, shot = armed
        device.modify_block(2, GAIN_BLOCK, _set_gain(490))
        device.modify_block(2, 0, lambda b: bytes([9]) + bytes(b[1:]))
        fake.writes.clear()

        report = snap.restore(device, shot, dry_run=False)
        assert report.total_writes == 2
        assert len(fake.writes) == 2

    def test_restoring_a_matching_device_writes_nothing(self, armed):
        device, fake, shot = armed
        report = snap.restore(device, shot, dry_run=False)
        assert report.total_writes == 0
        assert report.device_matches
        assert report.clean
        assert fake.writes == []

    def test_it_is_idempotent(self, armed):
        # Safely re-runnable after a partial failure.
        device, fake, shot = armed
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        snap.restore(device, shot, dry_run=False)
        fake.writes.clear()
        second = snap.restore(device, shot, dry_run=False)
        assert second.total_writes == 0
        assert fake.writes == []

    def test_a_single_channel_can_be_restored(self, armed):
        device, fake, shot = armed
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        device.modify_block(1, GAIN_BLOCK, _set_gain(490))
        report = snap.restore(device, shot, outputs=[0], dry_run=False)
        assert len(report.channels) == 1
        assert int.from_bytes(fake.image.channels[1][250:252], "little") == 490

    def test_it_refuses_a_bad_output(self, armed):
        device, _, shot = armed
        with pytest.raises(snap.SnapshotError, match="outside"):
            snap.restore(device, shot, outputs=[9])

    def test_a_contradicted_block_stops_everything(self, armed):
        # A difference in a block whose meaning is contradicted is not
        # something to write through. Nothing is transmitted, and a person
        # gets involved.
        device, fake, shot = armed
        fake.image.channels[0][34 * 8 : 35 * 8] = bytes([0xFF] * 8)
        fake.writes.clear()
        with pytest.raises(snap.RestoreBlocked, match="Escalate"):
            snap.restore(device, shot, dry_run=False)
        assert fake.writes == []

    def test_it_stops_when_a_channel_fails_to_verify(self, armed):
        device, fake, shot = armed
        for ch in range(8):
            device.modify_block(ch, GAIN_BLOCK, _set_gain(490))

        # Channel 0 restores; channel 1's write is swallowed.
        original = fake._write

        def swallow(frame):
            if int(frame.channel_id) == 1:
                return
            original(frame)

        fake._write = swallow
        with pytest.raises(ReadbackMismatch):
            snap.restore(device, shot, dry_run=False)
        # Channel 0 was still put right, and later channels were not touched.
        assert int.from_bytes(fake.image.channels[0][250:252], "little") == 500
        assert int.from_bytes(fake.image.channels[7][250:252], "little") == 490

    def test_writes_still_require_arming(self, tmp_path, rig):
        device, fake, identity = rig
        shot = snap.capture(device, identity, now=0.0)
        fake.image.channels[0][250] = 0xAB
        with pytest.raises(WritesNotArmed):
            snap.restore(device, shot, dry_run=False)


class TestDdpInterop:
    """The independent restore path, through the vendor app."""

    def test_serialize_round_trips_every_backup_in_the_repo(self):
        files = sorted(REPO.glob("*.DDP"))
        assert files, "repo should hold vendor backups"
        for path in files:
            raw = path.read_bytes()
            assert ddp.serialize(ddp.parse(raw)) == raw, path.name

    @pytest.mark.parametrize(
        "name",
        [
            "eq_1_baseline.DDP",
            "eq_3_bypass_off.DDP",
            "dspcartunebackups_Channel4_preset.DDP",
        ],
    )
    def test_splicing_a_matching_snapshot_reproduces_the_file(self, name):
        # Known-answer test: these backups already hold exactly the records
        # the capture read back, so splicing them in must be a no-op.
        path = REPO / name
        original = path.read_bytes()
        backup = ddp.parse(original)
        records = [
            b"".join(
                backup.blocks[ddp.OUTPUT_SECTION_START + ch * ddp.BLOCKS_PER_OUTPUT + i]
                for i in range(ddp.BLOCKS_PER_OUTPUT)
            )
            for ch in range(8)
        ]
        assert ddp.splice_outputs(backup, records) == original

    def test_a_snapshot_can_be_written_as_a_loadable_ddp(self, tmp_path, rig):
        device, _, identity = rig
        shot = snap.capture(device, identity, now=0.0)
        template = ddp.parse((REPO / "eq_1_baseline.DDP").read_bytes())

        blob = shot.to_ddp(template)
        rebuilt = ddp.parse(blob)

        # The outputs are ours; everything undecoded came from the template.
        for ch in range(8):
            start = ddp.OUTPUT_SECTION_START + ch * ddp.BLOCKS_PER_OUTPUT
            record = b"".join(
                rebuilt.blocks[start + i] for i in range(ddp.BLOCKS_PER_OUTPUT)
            )
            assert record == shot.channels[ch]
        assert rebuilt.global_blocks == template.global_blocks
        assert rebuilt.input_blocks(0) == template.input_blocks(0)

    def test_splice_rejects_the_wrong_number_of_records(self):
        template = ddp.parse((REPO / "eq_1_baseline.DDP").read_bytes())
        with pytest.raises(ProtocolError, match="need 8"):
            ddp.splice_outputs(template, [bytes(296)] * 7)

    def test_splice_rejects_a_wrong_record_length(self):
        template = ddp.parse((REPO / "eq_1_baseline.DDP").read_bytes())
        with pytest.raises(ProtocolError, match="expected 296"):
            ddp.splice_outputs(template, [bytes(100)] * 8)


class TestReportsDoNotOverclaim:
    def test_clean_describes_the_device_not_the_run(self, armed):
        device, _, shot = armed
        device.modify_block(0, GAIN_BLOCK, _set_gain(490))
        report = snap.restore(device, shot, dry_run=True)
        # The dry run itself succeeded, but the device does not match.
        assert not report.clean
        assert not report.device_matches

    def test_linked_channels_are_reported_from_the_snapshot(self, tmp_path):
        image = DeviceImage.flat()
        image.channels[6][280 + 7] = 1
        image.channels[7][280 + 7] = 1
        device, _, identity = _rig(tmp_path, image)
        shot = snap.capture(device, identity, now=0.0)
        assert shot.linked_channels() == {6, 7}


class TestBringUpRehearsal:
    """The operator-facing bring-up script, run end to end.

    ``tools/dsp408_probe.py rehearse`` is what gets run against the fake before
    anything is run against hardware. It is the only place the stages are
    exercised *in sequence*, in one process, the way they will happen on the
    bench -- so if it rots, the rehearsal stops meaning anything and nobody
    finds out until the DSP is on the desk.
    """

    def test_every_stage_passes(self, tmp_path, capsys):
        import importlib.util

        tool = REPO / "tools" / "dsp408_probe.py"
        spec = importlib.util.spec_from_file_location("dsp408_probe", tool)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)

        args = probe.argparse.Namespace(
            fake=True,
            address=None,
            port=None,
            channel=1,
            journal=None,
            session_id="rehearsal",
            workdir=str(tmp_path),
        )
        assert probe.cmd_rehearse(args) == 0

        out = capsys.readouterr().out
        assert "steps passed" in out
        assert "[FAIL]" not in out
        # The abort paths must actually have been reached, not skipped.
        for expected in (
            "WritesNotArmed",
            "UnverifiedBlock",
            "RestoreBlocked",
            "ReadbackMismatch",
        ):
            assert expected in out, f"{expected} path was not exercised"


class TestPresetRollback:
    """Rollback by preset recall -- eight reads instead of dozens of writes.

    The mechanism the improvement invariant has described since the start
    without having. Measured 2026-08-09; see ``docs/dsp408-protocol.md``.
    """

    @staticmethod
    def _preset_armed(armed):
        device, fake, shot = armed
        device.session.policy.allow_presets = True
        return device, fake, shot

    def test_store_then_recall_restores_every_channel(self, armed):
        device, fake, shot = self._preset_armed(armed)
        snap.store_as_preset(device, shot, slot=2, name="baseline")

        # Wreck the working area the way a failed tuning run would.
        for ch in range(8):
            device.modify_block(ch, GAIN_BLOCK, _set_gain(400))
        assert snap.compare(device, shot) != {}

        report = snap.restore_from_preset(device, 2, expect=shot, dry_run=False)
        assert report.device_matches
        assert snap.compare(device, shot) == {}
        assert "0 blocks differ" in report.summary()

    def test_a_dry_run_touches_nothing(self, armed):
        device, fake, shot = self._preset_armed(armed)
        before = fake.image.snapshot()
        report = snap.restore_from_preset(device, 2)
        assert report.dry_run
        assert fake.image.snapshot() == before
        assert fake.recalls == []

    def test_it_reports_a_mismatch_rather_than_claiming_success(self, armed):
        # The recall returning eight records is not evidence it applied them.
        # Only the re-read is, so a slot holding the wrong tune must surface
        # as a failure and not as a clean rollback.
        device, fake, shot = self._preset_armed(armed)
        device.modify_block(0, GAIN_BLOCK, _set_gain(400))
        fake.store_preset(4)  # slot 4 now holds the *wrong* tune

        report = snap.restore_from_preset(device, 4, expect=shot, dry_run=False)
        assert not report.device_matches
        assert report.differing == {0: [GAIN_BLOCK]}
        assert "still differ" in report.summary()

    def test_a_store_preserves_the_undecoded_blocks(self, armed):
        # 34 and 35 are refused for working-area writes because their meaning
        # is contradicted. A preset store must carry them anyway: it
        # round-trips a record the device produced, and dropping two blocks
        # would store a tune that is not the one captured.
        device, fake, shot = self._preset_armed(armed)
        snap.store_as_preset(device, shot, slot=5, name="baseline")
        stored = fake.image.presets[4]
        for ch in range(8):
            record = stored[ch * 296 : (ch + 1) * 296]
            assert record == shot.channels[ch]

    def test_presets_are_refused_unless_the_policy_says_so(self, armed):
        device, _, shot = armed  # note: not _preset_armed
        with pytest.raises(TxRefused):
            snap.store_as_preset(device, shot, slot=2, name="baseline")


class TestGlobalStateComparison:
    """The master volume, and why comparing only output records is not enough.

    Raised by an adversarial review on 2026-08-09, which claimed the master
    volume was "captured nowhere". **That part was wrong** -- the connect ritual
    reads `DataType 9` channel 5 and it lands in `snapshot.system_blocks[5]`.

    The rest of the claim was right, and it is the part that matters: nothing
    *compared* it. `compare()` returns a per-channel map and globals have no
    channel, so a master volume that moved between a baseline and a
    verification measurement would have gone unnoticed -- and the improvement
    invariant would have attributed a level change to the tune.
    """

    def test_no_drift_reports_nothing(self, armed):
        device, _, shot = armed
        assert snap.compare_system(device, shot) == {}

    def test_a_master_volume_change_is_caught(self, armed):
        device, fake, shot = armed
        before = fake.image.system[snap.SYSTEM_MASTER_VOLUME]
        fake.image.system[snap.SYSTEM_MASTER_VOLUME] = bytes([0]) + before[1:]

        drift = snap.compare_system(device, shot)
        assert snap.SYSTEM_MASTER_VOLUME in drift
        stored, live = drift[snap.SYSTEM_MASTER_VOLUME]
        assert stored == before
        assert live[0] == 0

    def test_the_output_records_alone_would_have_missed_it(self, armed):
        # The whole point: a global moved and every channel record is identical.
        device, fake, shot = armed
        before = fake.image.system[snap.SYSTEM_MASTER_VOLUME]
        fake.image.system[snap.SYSTEM_MASTER_VOLUME] = bytes([0]) + before[1:]
        assert snap.compare(device, shot) == {}
        assert snap.compare_system(device, shot) != {}
