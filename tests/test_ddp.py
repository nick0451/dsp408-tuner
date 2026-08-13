"""Tests for the vendor ``.DDP`` backup reader.

The byte layout is pinned deliberately: a transposed field decodes a plausible
value into the wrong parameter, and the whole point of reading these files is
to learn encodings from them. A silently wrong parse would teach us something
false and look entirely reasonable doing it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tuner.dsp.ddp import (
    BLOCK_LEN,
    BLOCKS_PER_OUTPUT,
    HEADER_LEN,
    INPUT_SECTION_START,
    MAGIC,
    OUTPUT_SECTION_START,
    TOTAL_BLOCKS,
    TRAILING_SECTION_START,
    DdpEqBand,
    diff,
    parse,
)
from tuner.dsp.protocol import EQ_FREQ_TABLE_HZ, OUTPUT_BULK_LEN, ProtocolError

#: The real backup, if it is present. Absent in a fresh clone.
CORPUS = Path(__file__).resolve().parents[1] / "corpus"

REAL_BACKUP = CORPUS / "dspcartunebackups.DDP"

#: The same tune with output 1 reset to flat, saved 2026-08-08.
FLAT_BACKUP = CORPUS / "dspcartunebackups_flat_channel_1_diff.DDP"


def _blank_file(preset: str = "Custom") -> bytearray:
    data = bytearray()
    data.append(len(MAGIC))
    data += MAGIC
    data += preset.encode("ascii").ljust(32, b"\x00")
    data += bytes(TOTAL_BLOCKS * BLOCK_LEN)
    assert len(data) == HEADER_LEN + TOTAL_BLOCKS * BLOCK_LEN
    return data


def _set_block(data: bytearray, block_index: int, payload: bytes) -> None:
    assert len(payload) == BLOCK_LEN
    start = HEADER_LEN + block_index * BLOCK_LEN
    data[start : start + BLOCK_LEN] = payload


def _output_block(output: int, offset: int) -> int:
    return OUTPUT_SECTION_START + output * BLOCKS_PER_OUTPUT + offset


class TestLayout:
    def test_output_record_matches_the_wire_bulk_length(self):
        # 37 blocks of 8 bytes is exactly the DataLen the vendor uses to write
        # a whole output channel. If these ever disagree, one of them is wrong.
        assert BLOCKS_PER_OUTPUT * BLOCK_LEN == OUTPUT_BULK_LEN

    def test_sections_tile_the_file_exactly(self):
        assert len(parse(bytes(_blank_file())).blocks) == TOTAL_BLOCKS


class TestParse:
    def test_rejects_bad_magic(self):
        data = _blank_file()
        data[1:5] = b"XXXX"
        with pytest.raises(ProtocolError, match="bad magic"):
            parse(bytes(data))

    def test_rejects_truncated_file(self):
        with pytest.raises(ProtocolError, match="too short"):
            parse(bytes(_blank_file())[:10])

    def test_rejects_wrong_block_count(self):
        data = _blank_file() + bytes(BLOCK_LEN)
        with pytest.raises(ProtocolError, match="expected .* blocks"):
            parse(bytes(data))

    def test_rejects_partial_block(self):
        data = _blank_file() + b"\x00"
        with pytest.raises(ProtocolError, match="whole number of blocks"):
            parse(bytes(data))

    def test_reads_preset_name(self):
        assert parse(bytes(_blank_file("Tune A"))).preset_name == "Tune A"

    def test_misc_block_field_order(self):
        data = _blank_file()
        # mute/enable, polar, gain u16 LE, delay u16 LE, eq_mode, spk_type
        misc = bytes([1, 1, 0xE0, 0x01, 0x6A, 0x00, 0, 5])
        _set_block(data, _output_block(0, 31), misc)
        out = parse(bytes(data)).outputs[0]
        assert (out.enabled, out.polar) == (1, 1)
        assert out.gain_raw == 480
        assert out.delay_samples == 106
        assert (out.eq_mode, out.spk_type) == (0, 5)

    def test_crossover_block_is_asymmetric(self):
        # Frequencies are u16 but filter and slope are single bytes. Reading
        # the block as a uniform u16 array shifts every field after the first.
        data = _blank_file()
        xover = bytes([0xC2, 0x01, 0, 3, 0xC4, 0x09, 0, 3])
        _set_block(data, _output_block(0, 32), xover)
        out = parse(bytes(data)).outputs[0]
        assert (out.h_freq_hz, out.h_filter, out.h_level) == (450, 0, 3)
        assert (out.l_freq_hz, out.l_filter, out.l_level) == (2500, 0, 3)

    def test_eq_band_field_order(self):
        band = DdpEqBand.decode(bytes([0xAB, 0x21, 0x58, 0x02, 0x18, 0x00, 0x00, 0x02]))
        assert band == DdpEqBand(
            freq_hz=8619, level_raw=600, bw_raw=24, shf_db=0, type=2
        )

    def test_eq_bands_stop_before_the_misc_block(self):
        # Bands are 0..30 and misc is 31. An off-by-one here would read the
        # misc block as a 32nd band -- the same trap as on the wire.
        data = _blank_file()
        _set_block(data, _output_block(0, 31), bytes([1, 0, 0xE0, 1, 0, 0, 0, 0]))
        out = parse(bytes(data)).outputs[0]
        assert len(out.eq) == 31
        assert all(b.freq_hz == 0 for b in out.eq)

    def test_outputs_are_numbered_from_one(self):
        outputs = parse(bytes(_blank_file())).outputs
        assert [o.index for o in outputs] == list(range(1, 9))


class TestDiff:
    def test_no_differences_between_identical_files(self):
        a = parse(bytes(_blank_file()))
        assert diff(a, a) == []

    def test_reports_a_single_changed_output_field(self):
        before = _blank_file()
        after = _blank_file()
        _set_block(after, _output_block(2, 31), bytes([0, 0, 0xF4, 1, 0, 0, 0, 0]))
        changes = diff(parse(bytes(before)), parse(bytes(after)))
        assert changes == ["OUT3.gain_raw: 0 -> 500"]

    def test_reports_a_changed_eq_band(self):
        before = _blank_file()
        after = _blank_file()
        band = bytes([0xE8, 0x03, 0x58, 0x02, 0x34, 0, 0, 0])
        _set_block(after, _output_block(0, 4), band)
        changes = diff(parse(bytes(before)), parse(bytes(after)))
        assert changes == [
            "OUT1.eq[4].freq_hz: 0 -> 1000",
            "OUT1.eq[4].level_raw: 0 -> 600",
            "OUT1.eq[4].bw_raw: 0 -> 52",
        ]

    def test_reports_changes_in_undecoded_sections(self):
        # The input and global layouts are not decoded. A change landing there
        # must still be visible, or the app/file loop would silently lose it.
        before = _blank_file()
        after = _blank_file()
        _set_block(after, 40, bytes([1, 2, 3, 4, 5, 6, 7, 8]))
        changes = diff(parse(bytes(before)), parse(bytes(after)))
        assert changes == [
            "block[40] (input 1 block 7): "
            "00 00 00 00 00 00 00 00 -> 01 02 03 04 05 06 07 08"
        ]

    def test_preset_rename_is_reported(self):
        changes = diff(parse(bytes(_blank_file("A"))), parse(bytes(_blank_file("B"))))
        assert changes == ["preset name: 'A' -> 'B'"]


@pytest.fixture(scope="module")
def backup():
    return parse(REAL_BACKUP.read_bytes())


@pytest.mark.skipif(not REAL_BACKUP.exists(), reason="operator's backup not present")
class TestRealBackup:
    """Regression pins against the operator's actual saved tune.

    These are the values the bench session's hypotheses are built on, so a
    parser change that moves them should fail loudly.
    """

    def test_preset_name(self, backup):
        assert backup.preset_name == "Custom"

    def test_three_way_active_tune(self, backup):
        corners = [(o.h_freq_hz, o.l_freq_hz) for o in backup.outputs]
        assert corners == [
            (450, 2500),
            (450, 2500),  # midrange L/R
            (2500, 20000),
            (2500, 20000),  # tweeters L/R
            (55, 450),
            (55, 450),  # midbass L/R
            (20, 55),
            (20, 55),  # subs
        ]

    def test_delays_are_plausible_time_alignment(self, backup):
        delays = [o.delay_samples for o in backup.outputs]
        assert delays == [106, 9, 115, 14, 37, 0, 0, 0]

    def test_only_output_six_is_polarity_inverted(self, backup):
        assert [o.polar for o in backup.outputs] == [0, 0, 0, 0, 0, 1, 0, 0]

    def test_gain_raw_values(self, backup):
        gains = [o.gain_raw for o in backup.outputs]
        assert gains == [480, 500, 480, 500, 470, 470, 0, 0]

    def test_tune_uses_frequencies_absent_from_the_assumed_tables(self, backup):
        # The evidence behind the open quantization question. If this ever
        # stops holding, the reasoning in docs/STATE.md needs revisiting.
        from tuner.dsp.protocol import EQ_FREQ_TABLE_HZ, XOVER_FREQ_TABLE_HZ

        off_xover = {
            f
            for o in backup.outputs
            for f in (o.h_freq_hz, o.l_freq_hz)
            if f not in XOVER_FREQ_TABLE_HZ
        }
        off_eq = {
            b.freq_hz
            for o in backup.outputs
            for b in o.eq
            if b.freq_hz not in EQ_FREQ_TABLE_HZ
        }
        assert off_xover == {55, 450, 2500}
        assert len(off_eq) >= 20


@pytest.fixture(scope="module")
def flat_backup():
    return parse(FLAT_BACKUP.read_bytes())


@pytest.mark.skipif(
    not (REAL_BACKUP.exists() and FLAT_BACKUP.exists()),
    reason="operator's backups not present",
)
class TestResetToFlat:
    """What the vendor app's "reset to flat" does, and what it reveals.

    Output 1 was reset to flat between these two saves. The diff is the
    evidence behind two claims in ``docs/dsp408-protocol.md``, so it is pinned
    here rather than left to be re-derived.
    """

    def test_default_band_layout_is_octave_then_third_octave(self, flat_backup):
        bands = [b.freq_hz for b in flat_backup.outputs[0].eq]
        assert bands[:10] == [31, 65, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
        assert bands[10:] == list(EQ_FREQ_TABLE_HZ[10:31])

    def test_the_app_default_is_not_expressible_in_the_assumed_table(self, flat_backup):
        # 31 Hz and 65 Hz are the app's own defaults; the table holds 32 and 63.
        # A quantization grid that cannot express the defaults is not one.
        bands = [b.freq_hz for b in flat_backup.outputs[0].eq]
        assert 31 not in EQ_FREQ_TABLE_HZ
        assert 65 not in EQ_FREQ_TABLE_HZ
        assert bands[0] == 31
        assert bands[1] == 65

    def test_default_bandwidth_is_52(self, flat_backup):
        assert {b.bw_raw for b in flat_backup.outputs[0].eq} == {52}

    def test_reset_zeroes_delay_and_opens_the_crossover(self, flat_backup):
        out1 = flat_backup.outputs[0]
        assert out1.delay_samples == 0
        assert (out1.h_freq_hz, out1.l_freq_hz) == (20, 20000)

    def test_reset_does_not_touch_channel_gain(self, backup, flat_backup):
        assert flat_backup.outputs[0].gain_raw == backup.outputs[0].gain_raw == 480

    def test_out5_the_test_channel_is_untouched(self, backup, flat_backup):
        # The quantization measurement depends on OUT5 still holding the
        # original 450 Hz corner. If this fails, that test is invalid.
        before, after = backup.outputs[4], flat_backup.outputs[4]
        assert (after.h_freq_hz, after.l_freq_hz) == (55, 450)
        assert after == before

    def test_only_outputs_1_7_and_8_changed(self, backup, flat_backup):
        changed = {
            b.index
            for b, a in zip(backup.outputs, flat_backup.outputs, strict=True)
            if b != a
        }
        # 7 and 8 are the sub channels, which went from off to gain_raw 433.
        # That change was not made deliberately -- see docs/STATE.md.
        assert changed == {1, 7, 8}


POWERCYCLE_BEFORE = CORPUS / "dspcartunebackups_Channel4_preset_bypass_eq.DDP"
POWERCYCLE_AFTER = (
    CORPUS / "dspcartunebackups_Channel4_preset_bypass_eq_powercycled.DDP"
)


@pytest.mark.skipif(
    not (POWERCYCLE_BEFORE.exists() and POWERCYCLE_AFTER.exists()),
    reason="power-cycle experiment backups not present",
)
class TestPowerCycleEvidence:
    """The measurement behind the persistence model in docs/dsp408-protocol.md.

    Twelve EQ band gains were changed with no explicit save to the device, then
    power was pulled. Everything came back. This pins that evidence so the
    conclusion cannot quietly lose its support.
    """

    def test_every_parameter_survives_a_power_cycle(self):
        before = parse(POWERCYCLE_BEFORE.read_bytes())
        after = parse(POWERCYCLE_AFTER.read_bytes())
        assert before.outputs == after.outputs

    def test_only_the_name_string_differs(self):
        before = parse(POWERCYCLE_BEFORE.read_bytes())
        after = parse(POWERCYCLE_AFTER.read_bytes())
        # Everything past the header blocks is identical; the preset name is a
        # save-dialog artifact and carries no device state.
        assert before.blocks == after.blocks
        assert before.preset_name != after.preset_name


class TestDiffCoversUndecodedOutputBlocks:
    """A change anywhere must surface, including in fields we cannot name.

    An earlier version skipped the entire output section in the raw-block
    sweep, so the two undecoded dynamics blocks inside each output record were
    invisible. A tool whose whole purpose is "change one control and see what
    moved" must never answer "nothing moved" when something did.
    """

    def _dynamics_block(self, output: int) -> int:
        return OUTPUT_SECTION_START + output * BLOCKS_PER_OUTPUT + 34

    def test_reports_a_change_in_an_undecoded_dynamics_block(self):
        before = _blank_file()
        after = _blank_file()
        _set_block(after, self._dynamics_block(2), bytes([9, 8, 7, 6, 5, 4, 3, 2]))
        changes = diff(parse(bytes(before)), parse(bytes(after)))
        assert len(changes) == 1
        assert "OUT3 dynamics A, undecoded" in changes[0]

    def test_decoded_changes_are_not_also_reported_raw(self):
        # Every differing block is reported exactly once: as named fields when
        # we understand it, as raw bytes when we do not.
        before = _blank_file()
        after = _blank_file()
        _set_block(after, _output_block(0, 31), bytes([0, 0, 0xF4, 1, 0, 0, 0, 0]))
        changes = diff(parse(bytes(before)), parse(bytes(after)))
        assert changes == ["OUT1.gain_raw: 0 -> 500"]

    def test_partly_decoded_blocks_still_report_their_raw_bytes(self):
        # Only byte 7 of the dynamics B block is decoded. Suppressing its raw
        # line because one field was named would silently lose the other seven.
        before, after = _blank_file(), _blank_file()
        _set_block(after, _output_block(6, 35), bytes([1, 1, 1, 1, 1, 1, 1, 1]))
        changes = diff(parse(bytes(before)), parse(bytes(after)))
        assert "OUT7.linkgroup_num: 0 -> 1" in changes
        assert any("only linkgroup_num decoded" in c for c in changes)

    def test_locations_are_named_for_every_section(self):
        for block, expected in (
            (5, "global block 5"),
            (INPUT_SECTION_START + 3, "input 1 block 3"),
            (OUTPUT_SECTION_START + 34, "OUT1 dynamics A, undecoded"),
            (OUTPUT_SECTION_START + 35, "OUT1 dynamics B, only linkgroup_num"),
            (TRAILING_SECTION_START + 1, "trailing block 1"),
        ):
            before, after = _blank_file(), _blank_file()
            _set_block(after, block, bytes([1, 1, 1, 1, 1, 1, 1, 1]))
            changes = diff(parse(bytes(before)), parse(bytes(after)))
            assert any(expected in c for c in changes), (block, changes)


EQ_LADDER = {
    name: CORPUS / f"eq_{name}.DDP"
    for name in ("1_baseline", "2_bypass_on", "3_bypass_off", "4_reset", "5_restore")
}


@pytest.mark.skipif(
    not all(p.exists() for p in EQ_LADDER.values()),
    reason="EQ control ladder backups not present",
)
class TestEqControlLadder:
    """Evidence for what each of the app's three EQ controls does.

    These distinguish bypass from reset by byte signature, which is the only
    thing that reliably did -- recollection got it wrong twice.
    """

    def _load(self, name):
        return parse(EQ_LADDER[name].read_bytes())

    def test_bypass_zeroes_levels_and_nothing_else(self):
        base, bypassed = self._load("1_baseline"), self._load("2_bypass_on")
        for before, after in zip(base.outputs, bypassed.outputs, strict=True):
            for b_band, a_band in zip(before.eq, after.eq, strict=True):
                assert b_band.freq_hz == a_band.freq_hz
                assert b_band.bw_raw == a_band.bw_raw
                assert a_band.level_raw == 600

    def test_bypass_round_trips_exactly(self):
        assert self._load("1_baseline").outputs == self._load("3_bypass_off").outputs

    def test_reset_rewrites_the_whole_band_layout(self):
        # This is what separates reset from bypass: it moves frequency and
        # bandwidth too, not just level.
        reset = self._load("4_reset")
        assert [b.freq_hz for b in reset.outputs[0].eq[:10]] == [
            31,
            65,
            125,
            250,
            500,
            1000,
            2000,
            4000,
            8000,
            16000,
        ]
        assert {b.bw_raw for o in reset.outputs for b in o.eq} == {52}
        assert {b.level_raw for o in reset.outputs for b in o.eq} == {600}

    def test_restore_does_not_undo_a_reset(self):
        assert self._load("4_reset").outputs == self._load("5_restore").outputs
