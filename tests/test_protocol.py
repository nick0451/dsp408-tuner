"""Tests for the DSP-408 wire protocol codec.

The reference for every constant here is the decompiled vendor app; see
`docs/dsp408-protocol.md`. Nothing in this file talks to hardware.

The destructive-command tests matter most. Those opcodes sit among ordinary
parameter ChannelIDs, and the whole reason for decoding the protocol before
touching the device was to know which ones not to send.
"""

import pytest

from tuner.dsp.protocol import (
    BW_OCTAVES_MAX,
    BW_OCTAVES_MIN,
    BW_RAW_MAX,
    BW_RAW_MIN,
    DEVICE_EQ_GAIN_DB,
    DEVICE_Q_MAX,
    DEVICE_Q_MIN,
    EQ_FREQ_TABLE_HZ,
    FRAME_END,
    FRAME_START,
    OVERHEAD,
    UNITY_RAW,
    VENDOR_HEAD,
    XOVER_FREQ_TABLE_HZ,
    ChecksumError,
    DataType,
    DestructiveCommand,
    EqBand,
    Frame,
    FrameType,
    ProtocolError,
    UnsendableFrame,
    bandwidth_octaves,
    bw_raw_for_q,
    checksum,
    decode,
    gain_dbfs,
    gain_raw_for,
    nearest_eq_index,
    nearest_xover_index,
    q_from_bw_raw,
)


def write_frame(**kw) -> Frame:
    base = dict(
        frame_type=FrameType.WRITE,
        data_type=DataType.SYSTEM,
        channel_id=5,
        payload=bytes(range(8)),
    )
    base.update(kw)
    return Frame(**base)


class TestFraming:
    def test_header_layout(self):
        raw = write_frame().encode()
        assert raw[0:3] == VENDOR_HEAD
        assert raw[3] == FRAME_START
        assert raw[4] == FrameType.WRITE
        assert raw[7] == DataType.SYSTEM
        assert raw[8] == 5

    def test_total_length_is_payload_plus_overhead(self):
        for n in (0, 1, 8, 136, 528):
            raw = Frame(
                frame_type=FrameType.WRITE,
                data_type=3,
                channel_id=0,
                payload=bytes(n),
            ).encode()
            assert len(raw) == n + OVERHEAD

    def test_length_field_is_little_endian(self):
        raw = Frame(
            frame_type=FrameType.WRITE,
            data_type=3,
            channel_id=0,
            payload=bytes(528),
        ).encode()
        assert raw[12] == 528 & 0xFF
        assert raw[13] == 528 >> 8

    def test_frame_ends_with_aa(self):
        assert write_frame().encode()[-1] == FRAME_END

    def test_read_frames_carry_no_payload(self):
        # The vendor app forces DataLen = 0 for reads.
        with pytest.raises(ValueError, match="read frames carry no payload"):
            Frame(
                frame_type=FrameType.READ,
                data_type=9,
                channel_id=0,
                payload=b"\x01",
            )

    def test_read_frame_is_minimum_length(self):
        raw = Frame(
            frame_type=FrameType.READ, data_type=DataType.SYSTEM, channel_id=5
        ).encode()
        assert len(raw) == OVERHEAD
        assert raw[4] == FrameType.READ


class TestChecksum:
    def test_covers_type_through_payload(self):
        raw = write_frame().encode()
        n = len(raw) - OVERHEAD
        expected = 0
        for b in raw[4 : 14 + n]:
            expected ^= b
        assert raw[14 + n] == expected

    def test_vendor_head_is_excluded(self):
        # Checksum starts at offset 4, so the 0x80 0x80 0x80 0xEE preamble
        # must not affect it.
        raw = write_frame().encode()
        n = len(raw) - OVERHEAD
        assert checksum(raw, n) == checksum(b"\x00\x00\x00\x00" + raw[4:], n)

    def test_zero_checksum_is_refused(self):
        # A frame whose XOR happens to cancel cannot be sent: the vendor app
        # bails out rather than transmitting it, so some parameter
        # combinations are simply unsendable as a single frame.
        unsendable = []
        for pad in range(256):
            frame = Frame(
                frame_type=FrameType.WRITE,
                data_type=9,
                channel_id=5,
                payload=bytes([pad]),
            )
            try:
                frame.encode()
            except UnsendableFrame:
                unsendable.append(pad)
        assert len(unsendable) == 1, (
            f"exactly one payload byte should cancel the XOR to zero; got {unsendable}"
        )

    def test_decode_rejects_a_corrupted_checksum(self):
        raw = bytearray(write_frame().encode())
        raw[-2] ^= 0xFF
        with pytest.raises(ChecksumError):
            decode(bytes(raw))


class TestRoundTrip:
    @pytest.mark.parametrize("payload", [b"", bytes(8), bytes(range(64))])
    def test_encode_decode_preserves_fields(self, payload):
        original = Frame(
            frame_type=FrameType.WRITE,
            data_type=3,
            channel_id=7,
            data_id=11,
            device_id=1,
            user_id=0,
            bluetooth_device_id=2,
            pc_custom=0,
            payload=payload,
        )
        assert decode(original.encode()) == original


class TestDecodeGuards:
    def test_rejects_short_frame(self):
        with pytest.raises(ProtocolError, match="minimum"):
            decode(b"\x80\x80\x80\xee")

    def test_rejects_bad_vendor_head(self):
        raw = bytearray(write_frame().encode())
        raw[0] = 0x00
        with pytest.raises(ProtocolError, match="vendor head"):
            decode(bytes(raw))

    def test_rejects_bad_frame_start(self):
        raw = bytearray(write_frame().encode())
        raw[3] = 0x00
        with pytest.raises(ProtocolError, match="frame start"):
            decode(bytes(raw))

    def test_rejects_bad_frame_end(self):
        raw = bytearray(write_frame().encode())
        raw[-1] = 0x00
        with pytest.raises(ProtocolError, match="frame end"):
            decode(bytes(raw))

    def test_rejects_length_mismatch(self):
        raw = bytearray(write_frame().encode())
        raw[12] = 99
        with pytest.raises(ProtocolError, match="length field"):
            decode(bytes(raw))

    def test_rejects_oversized_field(self):
        with pytest.raises(ValueError, match="does not fit in one byte"):
            Frame(frame_type=FrameType.WRITE, data_type=9, channel_id=999)


class TestDestructiveCommands:
    @pytest.mark.parametrize(
        ("channel_id", "name"),
        [(96, "RESET_MCU"), (97, "TRANSMITTAL"), (98, "RESET_GROUP_DATA")],
    )
    def test_refused_by_default(self, channel_id, name):
        f = Frame(
            frame_type=FrameType.WRITE,
            data_type=DataType.SYSTEM,
            channel_id=channel_id,
            payload=bytes(8),
        )
        assert f.destructive == name
        with pytest.raises(DestructiveCommand, match=name):
            f.encode()

    def test_encodable_only_with_explicit_opt_in(self):
        f = Frame(
            frame_type=FrameType.WRITE,
            data_type=DataType.SYSTEM,
            channel_id=96,
            payload=bytes(8),
        )
        assert len(f.encode(allow_destructive=True)) == 8 + OVERHEAD

    def test_neighbouring_channel_ids_are_ordinary(self):
        # 95 and 99 are normal parameter blocks. The destructive ones sit
        # right among them, which is why they are named rather than trusted
        # to be memorable.
        for channel_id in (95, 99):
            f = Frame(
                frame_type=FrameType.WRITE,
                data_type=DataType.SYSTEM,
                channel_id=channel_id,
                payload=bytes(8),
            )
            assert f.destructive is None
            f.encode()

    def test_same_channel_id_on_another_data_type_is_safe(self):
        f = Frame(
            frame_type=FrameType.WRITE,
            data_type=DataType.INPUT_CHANNEL,
            channel_id=96,
            payload=bytes(8),
        )
        assert f.destructive is None


class TestEqBand:
    def test_round_trip(self):
        band = EqBand(freq=17, level=1234, bw=56, shf_db=3, type=1)
        assert EqBand.decode(band.encode()) == band

    def test_is_eight_bytes(self):
        assert len(EqBand(freq=0, level=0, bw=0).encode()) == 8

    def test_little_endian_fields(self):
        raw = EqBand(freq=0x0102, level=0x0304, bw=0x0506).encode()
        assert raw[0:2] == b"\x02\x01"
        assert raw[2:4] == b"\x04\x03"
        assert raw[4:6] == b"\x06\x05"

    def test_rejects_short_input(self):
        with pytest.raises(ProtocolError, match="8 bytes"):
            EqBand.decode(b"\x00" * 7)


class TestFrequencyTables:
    def test_eq_table_is_31_bands(self):
        assert len(EQ_FREQ_TABLE_HZ) == 31

    def test_xover_table_is_51_points(self):
        assert len(XOVER_FREQ_TABLE_HZ) == 51

    def test_tables_are_ascending(self):
        for table in (EQ_FREQ_TABLE_HZ, XOVER_FREQ_TABLE_HZ):
            assert all(b > a for a, b in zip(table, table[1:], strict=False))

    def test_exact_table_frequencies_map_to_themselves(self):
        for i, f in enumerate(EQ_FREQ_TABLE_HZ):
            assert nearest_eq_index(f) == i

    def test_snapping_uses_log_distance(self):
        # Geometric midpoint of 1000 and 1250 is ~1118. Linear distance would
        # call that closer to 1000; log distance is what the ear and the table
        # both use.
        assert EQ_FREQ_TABLE_HZ[nearest_eq_index(1119.0)] == 1250
        assert EQ_FREQ_TABLE_HZ[nearest_eq_index(1117.0)] == 1000

    def test_out_of_range_clamps_to_the_ends(self):
        assert nearest_eq_index(5.0) == 0
        assert nearest_eq_index(30_000.0) == len(EQ_FREQ_TABLE_HZ) - 1

    def test_xover_snapping(self):
        assert XOVER_FREQ_TABLE_HZ[nearest_xover_index(2000.0)] == 2000
        assert XOVER_FREQ_TABLE_HZ[nearest_xover_index(80.0)] == 76

    def test_rejects_non_positive(self):
        with pytest.raises(ValueError, match="positive"):
            nearest_eq_index(0.0)


class TestMeasuredScaling:
    """Parameter scalings measured 2026-08-08 against the vendor app display.

    Pinned because they were expensive to obtain and a silent change to any of
    them would put wrong values on the wire while looking entirely reasonable.
    """

    @pytest.mark.parametrize(
        ("raw", "dbfs"),
        [
            (433, -16.7),
            (470, -13.0),
            (480, -12.0),
            (500, -10.0),
            (410, -19.0),
            (600, 0.0),
            (540, -6.0),
            (589, -1.1),
            (613, 1.3),
        ],
    )
    def test_gain_matches_the_app_display(self, raw, dbfs):
        assert gain_dbfs(raw) == pytest.approx(dbfs, abs=1e-9)

    def test_gain_round_trips(self):
        for raw in range(0, 1000, 7):
            assert gain_raw_for(gain_dbfs(raw)) == raw

    def test_output_gain_and_eq_level_share_one_encoding(self):
        # 600 is 0 dB in both fields; that shared origin is what made the
        # encoding believable rather than a curve fit.
        assert gain_dbfs(UNITY_RAW) == 0.0

    @pytest.mark.parametrize(
        ("bw_raw", "displayed_q"),
        [(24, 4.966), (43, 2.992), (52, 2.515), (90, 1.492), (134, 0.999)],
    )
    def test_q_matches_the_app_display(self, bw_raw, displayed_q):
        assert q_from_bw_raw(bw_raw) == pytest.approx(displayed_q, abs=0.001)

    def test_bandwidth_offset(self):
        # bw_raw 0 is 0.05 octaves, not 0. The default 52 is 0.57 octaves.
        assert bandwidth_octaves(0) == pytest.approx(0.05)
        assert bandwidth_octaves(52) == pytest.approx(0.57)

    @pytest.mark.parametrize(("q", "bw_raw"), [(1.0, 134), (3.0, 43), (1.5, 90)])
    def test_requested_q_snaps_the_way_the_app_snaps(self, q, bw_raw):
        assert bw_raw_for_q(q) == bw_raw

    def test_snapping_rounds_bandwidth_up_so_q_never_exceeds_the_request(self):
        # Q 1.5 is the discriminating case: ordinary rounding gives 89, which
        # would overshoot the requested Q. The device errs wide instead.
        assert bw_raw_for_q(1.5) == 90
        for q in (0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0):
            assert q_from_bw_raw(bw_raw_for_q(q)) <= q + 1e-9

    def test_q_resolution_is_non_uniform(self):
        # Why the optimizer must search bandwidth, not Q: one raw step is worth
        # two orders of magnitude more Q at the narrow end than the wide end.
        narrow = abs(q_from_bw_raw(10) - q_from_bw_raw(11))
        wide = abs(q_from_bw_raw(250) - q_from_bw_raw(251))
        assert narrow > 100 * wide

    def test_rejects_nonsense_q(self):
        with pytest.raises(ValueError, match="positive"):
            bw_raw_for_q(0.0)


class TestTheMeasuredBandwidthDomain:
    """`bw_raw` spans 0..295, and the endpoints re-derive the +5 offset.

    Measured 2026-08-12: the vendor app reports Q ranging 0.404 to 28.852.
    Those are not numbers anyone would choose, which is exactly what makes
    them evidence -- they are what `octaves = (raw + 5)/100` produces at the
    two ends of a clean integer range whose wide end is exactly 3.000 octaves.

    This is the third independent confirmation of that formula and the only
    one that pins the `+5`. The offset is what puts the narrow end at 0.05
    octaves rather than 0; getting it wrong shifts every bandwidth by a
    constant, which a fitter absorbs into neighbouring bands and nobody hears.
    """

    def test_the_narrow_end_matches_the_app(self):
        assert round(q_from_bw_raw(BW_RAW_MIN), 3) == DEVICE_Q_MAX
        assert bandwidth_octaves(BW_RAW_MIN) == pytest.approx(BW_OCTAVES_MIN)

    def test_the_wide_end_matches_the_app(self):
        assert round(q_from_bw_raw(BW_RAW_MAX), 3) == DEVICE_Q_MIN
        assert bandwidth_octaves(BW_RAW_MAX) == pytest.approx(BW_OCTAVES_MAX)

    def test_the_wide_end_is_exactly_three_octaves(self):
        # A clean endpoint on a clean range is what a real parameter looks
        # like, and it is the part that would not survive a wrong offset.
        assert bandwidth_octaves(BW_RAW_MAX) == 3.0

    def test_the_offset_is_what_makes_both_ends_work(self):
        # Pins the mechanism rather than the two numbers. Drop the +5 and the
        # narrow end becomes a division by zero or an infinite Q; keep it and
        # both ends land on the app's figures.
        assert bandwidth_octaves(0) == pytest.approx(0.05)
        assert bandwidth_octaves(295) == pytest.approx(3.00)
        assert bandwidth_octaves(0) > 0

    def test_the_fitter_stays_inside_the_device(self):
        # FitConstraints is deliberately narrower. If someone widens it past
        # the hardware this fails, which is the point.
        from tuner.optimize.biquad import DEFAULT_CONSTRAINTS

        assert DEFAULT_CONSTRAINTS.min_q >= DEVICE_Q_MIN
        assert DEFAULT_CONSTRAINTS.max_q <= DEVICE_Q_MAX
        assert DEFAULT_CONSTRAINTS.max_cut_db <= DEVICE_EQ_GAIN_DB
        assert DEFAULT_CONSTRAINTS.max_boost_db <= DEVICE_EQ_GAIN_DB
