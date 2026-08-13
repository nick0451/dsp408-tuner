"""The 296-byte bulk channel record, checked against two independent paths.

``DataType 4 / ChannelID 0..7 / DataID 119`` returns a complete output channel
in one reply. It is the snapshot primitive M3 will build rollback on, so it
needs to be right before anything writes.

The check available here is unusually strong and cost nothing to obtain. The
same 2368 bytes exist twice, produced by two paths that share no code:

* our RFCOMM bulk readback, recovered from ``captures/btsnoop_hci.log``;
* the Windows vendor app's own read-from-device, saved to a ``.DDP`` file.

Where the tune was the same, they agree byte for byte. That is the golden-frame
standard applied to the snapshot mechanism, and it was obtained without writing
a single byte to the only unit this project has.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tuner.dsp import ddp
from tuner.dsp.btsnoop import captured_frames
from tuner.dsp.protocol import (
    UNVERIFIED_OUTPUT_BLOCKS,
    DataType,
    EqBand,
    EqBandType,
    FrameType,
    OutputDynamics,
    OutputMisc,
    OutputXover,
    XoverAlignment,
    level_raw_for_slope,
    slope_db_per_octave,
)

REPO = Path(__file__).resolve().parents[1]
#: The vendor-app .DDP corpus. Evidence, not fixtures -- these are real
#: saves off a real device, and several decoded fields rest on the A/Bs
#: among them.
CORPUS = REPO / "corpus"
CAPTURE = REPO / "captures" / "btsnoop_hci.log"

#: DataID the app uses to read a whole output channel. Not in ``OutputBlock``
#: -- it is a read-only aggregate, and must never become a write opcode.
BULK_READ_DATA_ID = 119

#: 37 blocks of 8 bytes: EQ bands 0..30, then MISC, XOVER, MIX, 34, DYNAMICS,
#: NAME.
BULK_LEN = 296
BLOCKS_PER_RECORD = 37

#: ``.DDP`` layout: 33 global blocks, then 6 inputs of 36, then the outputs.
DDP_OUTPUT_BASE = 33 + 6 * 36

#: Backups whose output section was taken from the same device state as the
#: capture. The others in the repo are bypass/reset experiments and are
#: *expected* to differ -- that they do is part of the evidence.
MATCHING_BACKUPS = (
    "dspcartunebackups_Channel4_preset.DDP",
    "eq_1_baseline.DDP",
    "eq_3_bypass_off.DDP",
)

pytestmark = pytest.mark.skipif(not CAPTURE.exists(), reason="capture not present")


def _bulk_records() -> dict[int, bytes]:
    frames = captured_frames(CAPTURE.read_bytes())
    return {
        f.frame.channel_id: f.frame.payload
        for f in frames
        if f.frame.data_type == DataType.OUTPUT_CHANNEL
        and f.frame.data_id == BULK_READ_DATA_ID
        and len(f.frame.payload) == BULK_LEN
    }


def _ddp_records(name: str) -> dict[int, bytes]:
    backup = ddp.parse((CORPUS / name).read_bytes())
    out = {}
    for ch in range(8):
        start = DDP_OUTPUT_BASE + ch * BLOCKS_PER_RECORD
        out[ch] = b"".join(backup.blocks[start + i] for i in range(BLOCKS_PER_RECORD))
    return out


@pytest.fixture(scope="module")
def records() -> dict[int, bytes]:
    got = _bulk_records()
    assert sorted(got) == list(range(8)), "capture should hold all 8 channels"
    return got


def _block(record: bytes, index: int) -> bytes:
    return record[index * 8 : index * 8 + 8]


class TestRecordShape:
    def test_every_channel_returns_thirty_seven_blocks(self, records):
        for ch, rec in records.items():
            assert len(rec) == BULK_LEN, ch
            assert BULK_LEN == BLOCKS_PER_RECORD * 8

    @pytest.mark.parametrize(
        ("band", "expected"),
        [
            # The app's first write to each band carried these exact freq and
            # level values while changing only bandwidth. It could only have
            # got them by reading them from this record at this offset, so
            # this pins `offset = data_id * 8` and read-modify-write together,
            # against real traffic rather than against our own assumption.
            (2, (486, 600, 25)),
            (3, (2514, 480, 42)),
        ],
    )
    def test_offset_is_data_id_times_eight(self, records, band, expected):
        got = EqBand.decode(_block(records[0], band))
        assert (got.freq, got.level, got.bw) == expected

    def test_the_app_echoed_fields_it_was_not_editing(self, records):
        # Same fact from the other side: the first write to band 3 is the
        # record's band 3 with one field changed. A backend that sends a
        # partial block reverts everything it did not set.
        frames = captured_frames(CAPTURE.read_bytes())
        first_band3 = next(
            f.frame
            for f in frames
            if not f.received
            and f.frame.frame_type == FrameType.WRITE
            and f.frame.payload
            and f.frame.data_id == 3
        )
        stored = EqBand.decode(_block(records[0], 3))
        sent = EqBand.decode(first_band3.payload)
        assert (sent.freq, sent.level) == (stored.freq, stored.level)
        assert sent.bw != stored.bw

    def test_channel_zero_matches_the_writes_the_app_later_sent(self, records):
        # Channel 0's stored state at connect time is what the app must have
        # read before it could send whole-block writes carrying fields the
        # operator never touched. Pins read-modify-write to real traffic.
        misc = OutputMisc.decode(_block(records[0], 31))
        assert misc.gain_raw == 500
        assert misc.delay_raw == 144
        xover = OutputXover.decode(_block(records[0], 32))
        assert (xover.h_freq, xover.l_freq) == (450, 3500)


class TestBlock34Contradiction:
    """Blocks 34/35, and why neither may be written.

    ``protocol.OutputBlock`` calls 34 ``MIX_IN_9_16``; ``ddp.py`` calls it
    "dynamics A". These tests record what the device actually returns, so the
    disagreement cannot quietly resolve itself in the wrong direction.
    """

    def test_block_33_looks_like_a_mix_and_34_does_not(self, records):
        mix = _block(records[0], 33)
        # One byte per input, a stereo pair fed from inputs 1 and 3.
        assert mix == bytes([0x50, 0, 0x50, 0, 0, 0, 0, 0])
        assert _block(records[1], 33) == bytes([0, 0x50, 0, 0x50, 0, 0, 0, 0])
        # 34 is identical on every channel and is not mix-shaped.
        assert len({_block(r, 34) for r in records.values()}) == 1

    def test_block_34_decodes_as_dynamics(self, records):
        dyn = OutputDynamics.decode(_block(records[0], 34))
        assert (dyn.all_pass_q, dyn.attack_time, dyn.release_time) == (420, 56, 500)

    def test_34_and_35_differ_only_where_the_link_group_lives(self, records):
        # Not a duplicate: identical on channels 0-5, differing on 6 and 7 in
        # byte 7 alone -- the linkgroup_num, set on exactly that pair. So 34
        # carries the same fields as 35 without the link group.
        for ch in range(6):
            assert _block(records[ch], 34) == _block(records[ch], 35), ch
        for ch in (6, 7):
            a, b = _block(records[ch], 34), _block(records[ch], 35)
            assert a != b
            assert a[:7] == b[:7]
            assert OutputDynamics.decode(a).linkgroup_num == 0
            assert OutputDynamics.decode(b).linkgroup_num == 1

    def test_the_pair_is_marked_unwritable(self):
        assert set(UNVERIFIED_OUTPUT_BLOCKS) == {34, 35}


class TestAgreesWithTheVendorApp:
    """Two read paths sharing no code, on 2368 bytes each."""

    @pytest.mark.parametrize("name", MATCHING_BACKUPS)
    def test_all_eight_records_are_byte_identical(self, records, name):
        theirs = _ddp_records(name)
        for ch in range(8):
            assert records[ch] == theirs[ch], f"{name} channel {ch}"

    def test_a_different_tune_state_does_differ(self, records):
        # The comparison would be worthless if everything matched everything.
        # eq_2_bypass_on was saved with EQ bypassed, so its band levels are
        # zeroed and it must not match.
        theirs = _ddp_records("eq_2_bypass_on.DDP")
        assert any(records[ch] != theirs[ch] for ch in range(8))


MUTE_BEFORE = CORPUS / "eq_channel1_no_mute.DDP"
MUTE_AFTER = CORPUS / "eq_channel1_mute.DDP"


@pytest.fixture(scope="module")
def mute_pair():
    for path in (MUTE_BEFORE, MUTE_AFTER):
        if not path.exists():
            pytest.skip(f"{path.name} not present")
    return ddp.parse(MUTE_BEFORE.read_bytes()), ddp.parse(MUTE_AFTER.read_bytes())


class TestMuteIsAnInvertedEnable:
    """The A/B that settled byte 0 of the MISC block.

    Measured 2026-08-09. The operator muted output 1 in the vendor app and
    saved a backup either side. The pair is kept in the repository because it
    is the entire evidence for the field's *sense*, and getting that backwards
    silences a channel -- or unsilences one muted deliberately.
    """

    BEFORE = CORPUS / "eq_channel1_no_mute.DDP"
    AFTER = CORPUS / "eq_channel1_mute.DDP"

    def test_muting_changed_exactly_one_byte_in_the_whole_file(self, mute_pair):
        before, after = mute_pair
        differing = [
            i
            for i, (a, b) in enumerate(zip(before.blocks, after.blocks, strict=True))
            if a != b
        ]
        assert len(differing) == 1, differing
        # Output 1's MISC block: 33 global + 6*36 input, then block 31.
        assert differing[0] == DDP_OUTPUT_BASE + 31

    def test_the_sense_is_inverted_from_the_apk_name(self, mute_pair):
        # 1 while playing, 0 while muted. The APK calls this field "mute",
        # which would mean the opposite.
        before, after = mute_pair
        assert before.outputs[0].enabled == 1
        assert after.outputs[0].enabled == 0

    def test_gain_was_not_touched(self, mute_pair):
        # So muting is a real separate control, not a gain zeroing -- a
        # backend can set one without disturbing the other.
        before, after = mute_pair
        assert before.outputs[0].gain_raw == after.outputs[0].gain_raw == 500

    def test_no_other_channel_moved(self, mute_pair):
        before, after = mute_pair
        for ch in range(1, 8):
            assert before.outputs[ch] == after.outputs[ch], ch

    def test_the_survey_that_predicted_this_still_holds(self):
        # Almost every channel-record in the repository reads 1; the sole
        # exception is a channel that was switched off. That is what justified
        # the experiment, and it is what makes the single A/B conclusive rather
        # than suggestive: if the byte meant "mute", every *active* channel
        # would read 0.
        #
        # Assert the exception set, not a count. This test originally pinned
        # "111 of 112" and broke the moment a bench session added seven
        # backups -- a survey whose conclusion is unchanged should not fail
        # because the sample grew. The claim was never about the number.
        disabled = {
            (path.name, i)
            for path in sorted(CORPUS.glob("*.DDP"))
            if path.name not in {MUTE_BEFORE.name, MUTE_AFTER.name}
            for i, out in enumerate(ddp.parse(path.read_bytes()).outputs)
            if out.enabled != 1
        }
        assert disabled == {("dspcartunebackups.DDP", 6)}


class TestCrossoverSelectorsAreMapped:
    """The fourteen A/Bs that mapped ``h_filter``/``l_filter`` and
    ``h_level``/``l_level``, 2026-08-09.

    These bytes sat unmapped for the whole project because the corpus had no
    variation in them: every saved tune used Linkwitz-Riley, so the filter byte
    read 0 on all 112 channel-records, and the operator only ever used 12 and
    24 dB/octave, so the level byte only ever took 1 or 3. **Absence of
    variation, not absence of meaning** -- and no further staring at the
    existing backups could have produced this. Two minutes in the app did.

    Kept as a test because the whole mapping rests on these files, and because
    a single-control A/B is only evidence if the other controls really did not
    move.
    """

    SLOPES = ((6, 0), (12, 1), (18, 2), (24, 3))
    ALIGNMENTS = (("butterworth", 1), ("bessel", 2), ("defeat", 3))
    BASELINE = "dspcartunebackups_flat_channel_1_diff.DDP"

    @staticmethod
    def _xover(name: str) -> OutputXover:
        path = CORPUS / name
        if not path.exists():
            pytest.skip(f"{name} not present")
        blocks = ddp.parse(path.read_bytes()).blocks
        return OutputXover.decode(blocks[DDP_OUTPUT_BASE + 32])

    @pytest.mark.parametrize(("slope", "level"), SLOPES)
    def test_high_pass_slope(self, slope, level):
        got = self._xover(f"dspcartunebackups_c1_hpf{slope}db.DDP")
        assert got.h_level == level
        assert got.h_level == level_raw_for_slope(slope)
        assert slope_db_per_octave(got.h_level) == slope

    @pytest.mark.parametrize(("slope", "level"), SLOPES)
    def test_low_pass_slope(self, slope, level):
        got = self._xover(f"dspcartunebackups_c1_lpf{slope}db.DDP")
        assert got.l_level == level

    @pytest.mark.parametrize(("name", "value"), ALIGNMENTS)
    def test_high_pass_alignment(self, name, value):
        got = self._xover(f"dspcartunebackups_c1_hpf24db_{name}.DDP")
        assert got.h_filter == value

    @pytest.mark.parametrize(("name", "value"), ALIGNMENTS)
    def test_low_pass_alignment(self, name, value):
        got = self._xover(f"dspcartunebackups_c1_lpf24db_{name}.DDP")
        assert got.l_filter == value

    def test_linkwitz_riley_is_zero(self):
        # Which is why the byte read 0 on all 112 records and looked dead.
        got = self._xover(self.BASELINE)
        assert got.h_filter == got.l_filter == XoverAlignment.LINKWITZ_RILEY

    @pytest.mark.parametrize(("slope", "_level"), SLOPES)
    def test_a_high_pass_ab_leaves_the_low_pass_alone(self, slope, _level):
        # The two sides are orthogonal, and a single-control A/B is only
        # evidence if that is true. Checked in both directions.
        base = self._xover(self.BASELINE)
        got = self._xover(f"dspcartunebackups_c1_hpf{slope}db.DDP")
        assert (got.l_freq, got.l_filter, got.l_level) == (
            base.l_freq,
            base.l_filter,
            base.l_level,
        )

    @pytest.mark.parametrize(("slope", "_level"), SLOPES)
    def test_a_low_pass_ab_leaves_the_high_pass_alone(self, slope, _level):
        base = self._xover(self.BASELINE)
        got = self._xover(f"dspcartunebackups_c1_lpf{slope}db.DDP")
        assert (got.h_freq, got.h_filter, got.h_level) == (
            base.h_freq,
            base.h_filter,
            base.h_level,
        )

    def test_the_corner_frequencies_never_moved(self):
        # If changing a slope had also nudged a corner, the mapping would be
        # confounded and the acoustic corroboration below would be luck.
        base = self._xover(self.BASELINE)
        for slope, _ in self.SLOPES:
            for side in ("hpf", "lpf"):
                got = self._xover(f"dspcartunebackups_c1_{side}{slope}db.DDP")
                assert (got.h_freq, got.l_freq) == (base.h_freq, base.l_freq)

    def test_the_mapping_agrees_with_the_acoustic_measurement(self):
        # OUT5's 450 Hz low-pass fitted a Linkwitz-Riley 4th-order crossover to
        # 0.247 dB rms on 2026-08-09 (450.1 Hz measured). This table has to say
        # LR4 for that record, or one of the two results is wrong.
        blocks = ddp.parse((CORPUS / self.BASELINE).read_bytes()).blocks
        out5 = OutputXover.decode(blocks[DDP_OUTPUT_BASE + 4 * 37 + 32])
        assert out5.l_freq == 450
        assert out5.l_filter == XoverAlignment.LINKWITZ_RILEY
        assert slope_db_per_octave(out5.l_level) == 24


class TestEqBandTypeIsMapped:
    """``EqBand.type``, mapped 2026-08-09 by A/B in the vendor app.

    Another field that read 0 on all 112 channel-records and was treated as
    unknown. Same reason as the crossover selectors: **the corpus contained no
    shelves**, because the operator had never used one. Enabling the low shelf
    and the high shelf, separately and together, moved one byte each.

    This mapping is the one that *added* a refusal rather than removing one.
    """

    LS = "dspcartunebackups_c1_ls_en.DDP"
    HS = "dspcartunebackups_c1_hs_en.DDP"
    BOTH = "dspcartunebackups_c1_ls_and_hs_en.DDP"

    @staticmethod
    def _types(name: str) -> list[int]:
        path = CORPUS / name
        if not path.exists():
            pytest.skip(f"{name} not present")
        blocks = ddp.parse(path.read_bytes()).blocks
        return [
            EqBand.decode(blocks[DDP_OUTPUT_BASE + band]).type for band in range(31)
        ]

    def test_the_low_shelf_is_one(self):
        assert self._types(self.LS)[0] == EqBandType.LOW_SHELF

    def test_the_high_shelf_is_two(self):
        assert self._types(self.HS)[9] == EqBandType.HIGH_SHELF

    def test_enabling_both_sets_both_bytes(self):
        both = self._types(self.BOTH)
        assert both[0] == EqBandType.LOW_SHELF
        assert both[9] == EqBandType.HIGH_SHELF

    def test_each_ab_moved_exactly_one_band(self):
        # A single-control A/B is only evidence if it was single.
        ls, hs = self._types(self.LS), self._types(self.HS)
        both = self._types(self.BOTH)
        assert [i for i in range(31) if ls[i] != both[i]] == [9]
        assert [i for i in range(31) if hs[i] != both[i]] == [0]

    def test_every_other_band_stays_peq(self):
        for name in (self.LS, self.HS, self.BOTH):
            types = self._types(name)
            for band in range(31):
                if band in (0, 9):
                    continue
                assert types[band] == EqBandType.PEQ, (name, band)

    def test_peq_is_zero_which_is_why_the_field_looked_dead(self):
        # 0 on all 112 records was absence of variation, not absence of
        # meaning -- exactly as with the crossover selector bytes.
        assert EqBandType.PEQ == 0
        for path in sorted(CORPUS.glob("*.DDP")):
            if path.name in {self.LS, self.HS, self.BOTH}:
                continue
            blocks = ddp.parse(path.read_bytes()).blocks
            for ch in range(8):
                base = DDP_OUTPUT_BASE + ch * BLOCKS_PER_RECORD
                for band in range(31):
                    assert EqBand.decode(blocks[base + band]).type == EqBandType.PEQ
