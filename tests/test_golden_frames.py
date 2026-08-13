"""The wire protocol, checked against real device traffic.

Every other protocol test asserts that the codec agrees with our *reading* of
the vendor app. These assert it agrees with **the bytes the vendor app actually
put on the wire to a real DSP-408**, captured 2026-08-08 over classic Bluetooth
RFCOMM and decoded by :mod:`tuner.dsp.btsnoop`.

That distinction is the whole point. A misread of the decompiled source
produces a codec that is wrong in exactly the way our tests are wrong, and no
amount of self-consistent testing finds it. These frames are ground truth, and
they were obtained without writing a single byte to the only unit we have.

``tests/golden/dsp408_frames.json`` is a curated subset. The full capture --
5834 frames -- also re-encodes byte-identically; see
``docs/dsp408-protocol.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tuner.dsp.protocol import (
    FRAME_END,
    FRAME_START,
    OVERHEAD,
    VENDOR_HEAD,
    DataType,
    EqBand,
    OutputMisc,
    OutputXover,
    checksum,
    decode,
    gain_dbfs,
    q_from_bw_raw,
)

GOLDEN = Path(__file__).parent / "golden" / "dsp408_frames.json"


def _load() -> list[dict]:
    with GOLDEN.open(encoding="utf-8") as fh:
        return json.load(fh)["frames"]


GOLDEN_FRAMES = _load()
WRITES = [f for f in GOLDEN_FRAMES if f["frame_type"] == 0xA1]


def _raw(entry: dict) -> bytes:
    return bytes.fromhex(entry["hex"])


def _ids(entries: list[dict]) -> list[str]:
    return [f"{e['offset_s']}s-{e['note']}" for e in entries]


class TestFraming:
    @pytest.mark.parametrize("entry", GOLDEN_FRAMES, ids=_ids(GOLDEN_FRAMES))
    def test_decodes(self, entry):
        frame = decode(_raw(entry))
        assert int(frame.frame_type) == entry["frame_type"]
        assert frame.data_type == entry["data_type"]
        assert frame.channel_id == entry["channel_id"]
        assert frame.data_id == entry["data_id"]

    @pytest.mark.parametrize("entry", GOLDEN_FRAMES, ids=_ids(GOLDEN_FRAMES))
    def test_re_encodes_byte_identically(self, entry):
        # The one that matters. If our encoder and the vendor's disagree by a
        # single byte, this fails -- and finding that out here costs nothing,
        # whereas finding it out by transmitting costs the operator's only DSP.
        raw = _raw(entry)
        assert decode(raw).encode() == raw

    @pytest.mark.parametrize("entry", GOLDEN_FRAMES, ids=_ids(GOLDEN_FRAMES))
    def test_checksum_matches_the_device(self, entry):
        raw = _raw(entry)
        n = len(raw) - OVERHEAD
        assert checksum(raw, n) == raw[-2]

    @pytest.mark.parametrize("entry", GOLDEN_FRAMES, ids=_ids(GOLDEN_FRAMES))
    def test_delimiters(self, entry):
        raw = _raw(entry)
        assert raw[:3] == VENDOR_HEAD
        assert raw[3] == FRAME_START
        assert raw[-1] == FRAME_END

    def test_no_captured_frame_was_destructive(self):
        # If ordinary app traffic tripped the destructive-opcode guard, the
        # guard would be wrong and would block legitimate writes.
        for entry in GOLDEN_FRAMES:
            assert decode(_raw(entry)).destructive is None


class TestCapturedWrites:
    """Real parameter writes, decoded through the block codecs."""

    def test_every_write_is_a_whole_eight_byte_block(self):
        # Measured: the app never writes a single field. A backend that assumes
        # otherwise will revert whatever it did not set.
        assert WRITES, "fixture should contain parameter writes"
        for entry in WRITES:
            frame = decode(_raw(entry))
            assert frame.data_type == DataType.OUTPUT_CHANNEL
            assert len(frame.payload) == 8

    @pytest.mark.parametrize(
        ("note", "expected_gain_raw", "expected_dbfs"),
        [
            ("channel gain 480 = -12.0 dB", 480, -12.0),
            ("channel gain 500 = -10.0 dB", 500, -10.0),
            ("channel gain 520 = -8.0 dB", 520, -8.0),
        ],
    )
    def test_gain_scaling_against_the_wire(
        self, note, expected_gain_raw, expected_dbfs
    ):
        # The app displayed 48 / 50 / 52 with no dB anywhere in its UI. These
        # bytes are what it sent, and they pin dB = raw/10 - 60 to real traffic
        # rather than to a reading of a screenshot.
        (entry,) = [e for e in WRITES if e["note"] == note]
        misc = OutputMisc.decode(decode(_raw(entry)).payload)
        assert misc.gain_raw == expected_gain_raw
        assert gain_dbfs(misc.gain_raw) == pytest.approx(expected_dbfs)

    def test_an_off_table_crossover_frequency_went_out_verbatim(self):
        # 1234 Hz appears in neither frequency table read out of the APK.
        # The app sent it unaltered, which is the second, independent
        # confirmation that those tables are not device constraints.
        (entry,) = [e for e in WRITES if "1234" in e["note"]]
        xover = OutputXover.decode(decode(_raw(entry)).payload)
        assert xover.l_freq == 1234
        assert xover.h_freq == 450  # untouched by the low-pass edit

    def test_eq_level_encoding_against_the_wire(self):
        (entry,) = [e for e in WRITES if "+8.9 dB" in e["note"]]
        band = EqBand.decode(decode(_raw(entry)).payload)
        assert band.level == 689
        assert gain_dbfs(band.level) == pytest.approx(8.9)

    def test_eq_bandwidth_encoding_against_the_wire(self):
        # The app displayed Q 1.268. Feeding that displayed value back through
        # bw_raw_for_q gives 107, because the display is already rounded and
        # the ceil rule then overshoots. The device holds 106.
        (entry,) = [e for e in WRITES if "+8.9 dB" in e["note"]]
        band = EqBand.decode(decode(_raw(entry)).payload)
        assert band.bw == 106
        assert q_from_bw_raw(band.bw) == pytest.approx(1.268, abs=0.001)

    def test_a_write_carries_fields_it_did_not_change(self):
        # Changing the EQ centre frequency re-sent level and bandwidth too.
        (entry,) = [e for e in WRITES if "12699" in e["note"]]
        band = EqBand.decode(decode(_raw(entry)).payload)
        assert band.freq == 12699
        assert band.level == 689  # carried along from the previous edit
        assert band.bw == 106

    def test_channel_ids_are_zero_based(self):
        # Every write in this capture was to output 1, and every one carries
        # channel_id 0. Off-by-one here addresses the wrong speaker.
        assert {decode(_raw(e)).channel_id for e in WRITES} == {0}


class TestCapturedReads:
    def test_read_frames_carry_no_payload(self):
        # The vendor app forces DataLen = 0 on reads, and Frame refuses to
        # build a read frame with a payload. Confirmed against real traffic.
        for entry in GOLDEN_FRAMES:
            if entry["frame_type"] == 0xA2:
                assert decode(_raw(entry)).payload == b""

    def test_acknowledgements_are_bare(self):
        for entry in GOLDEN_FRAMES:
            if entry["frame_type"] == 0x51:
                assert decode(_raw(entry)).payload == b""

    def test_unknown_frame_types_survive_a_round_trip(self):
        # 0x51 and 0x53 are not in FrameType. The codec must carry them
        # through as integers rather than rejecting or coercing them, or a
        # backend cannot read the device's replies at all.
        replies = [e for e in GOLDEN_FRAMES if e["frame_type"] in (0x51, 0x53)]
        assert replies
        for entry in replies:
            raw = _raw(entry)
            frame = decode(raw)
            assert int(frame.frame_type) == entry["frame_type"]
            assert frame.encode() == raw


SECOND_CAPTURE = (
    Path(__file__).resolve().parents[1]
    / "captures"
    / "btsnoop_hci_2026-08-09_presets.log"
)


@pytest.fixture(scope="module")
def frames():
    from tuner.dsp.btsnoop import captured_frames

    if not SECOND_CAPTURE.exists():
        pytest.skip("capture not present")
    got = captured_frames(SECOND_CAPTURE.read_bytes())
    assert len(got) == 28_991
    return got


@pytest.mark.skipif(not SECOND_CAPTURE.exists(), reason="capture not present")
class TestSecondCapture:
    """The preset capture, re-encoded frame for frame.

    28 991 frames covering everything the first capture missed: preset store
    and recall, mute, the master volume, mix writes and the link block. Held to
    the same standard -- decode then encode must reproduce the original bytes
    exactly, or the codec is guessing about something.

    This capture also contains the one frame that forced a correction to
    :meth:`Frame.encode`, which is why it is worth its own test rather than
    trusting the first capture to have covered the codec.
    """

    def test_every_frame_re_encodes_byte_identically(self, frames):
        for f in frames:
            assert f.frame.encode(allow_destructive=True) == f.raw, f.frame

    def test_it_contains_a_zero_checksum_reply(self, frames):
        # The frame that disproved "a zero checksum is unsendable" as a
        # protocol invariant. The vendor app refuses to *send* one -- 0 of
        # 2918 host frames in the first capture, 0 here -- but the device
        # emitted this reply and our reader accepted it. Applying the app's
        # send rule to replies made the in-process fake refuse to answer a
        # read the real unit answers.
        zero = [f for f in frames if f.raw[-2] == 0]
        assert len(zero) == 1
        got = zero[0]
        assert got.received
        assert (got.frame.data_type, got.frame.channel_id) == (4, 2)
        assert got.frame.user_id == 6
        assert len(got.frame.payload) == 296

    def test_no_host_frame_has_a_zero_checksum(self, frames):
        # Which is why the guard stays on for the direction we transmit in.
        assert not [f for f in frames if not f.received and f.raw[-2] == 0]
