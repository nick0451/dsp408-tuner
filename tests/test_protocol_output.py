"""Tests for the output-channel protocol blocks (DataType 4).

Field order and widths are taken from the `DataType == 4` branch of
`DataOptUtil.SendDataToDevice`. These tests pin the byte layout, because a
transposed field would write a plausible value into the wrong parameter and
the device would accept it without complaint.
"""

import pytest

from tuner.dsp.protocol import (
    BLOCK_PAYLOAD_LEN,
    OUTPUT_EQ_BANDS,
    DataType,
    EqBand,
    FrameType,
    OutputBlock,
    OutputDynamics,
    OutputMisc,
    OutputXover,
    ProtocolError,
    decode,
    nearest_xover_index,
    read_output,
    write_output_dynamics,
    write_output_eq,
    write_output_misc,
    write_output_mix,
    write_output_xover,
)


class TestOutputMisc:
    def test_byte_layout(self):
        raw = OutputMisc(
            enabled=1,
            polar=1,
            gain_raw=0x0102,
            delay_raw=0x0304,
            eq_mode=5,
            spk_type=6,
        ).encode()
        assert raw == bytes([1, 1, 0x02, 0x01, 0x04, 0x03, 5, 6])

    def test_round_trip(self):
        v = OutputMisc(enabled=0, polar=1, gain_raw=900, delay_raw=277, eq_mode=2)
        assert OutputMisc.decode(v.encode()) == v

    def test_is_eight_bytes(self):
        assert len(OutputMisc().encode()) == BLOCK_PAYLOAD_LEN

    def test_rejects_short_input(self):
        with pytest.raises(ProtocolError, match="OutputMisc"):
            OutputMisc.decode(bytes(7))


class TestOutputXover:
    def test_byte_layout(self):
        # Frequencies are 16-bit; filter and level are single bytes. Getting
        # this asymmetry wrong would silently shift every later field.
        raw = OutputXover(
            h_freq=0x0102,
            h_filter=3,
            h_level=4,
            l_freq=0x0506,
            l_filter=7,
            l_level=8,
        ).encode()
        assert raw == bytes([0x02, 0x01, 3, 4, 0x06, 0x05, 7, 8])

    def test_round_trip(self):
        v = OutputXover(h_freq=27, h_filter=2, h_level=24, l_freq=40, l_filter=1)
        assert OutputXover.decode(v.encode()) == v

    def test_frequencies_are_table_indices_not_hertz(self):
        # A crossover at 2 kHz is written as its *table index*, a small
        # number, not as 2000. Encoding the frequency directly would land far
        # outside the table and be silently accepted.
        idx = nearest_xover_index(2000.0)
        assert idx < 64, "table indices are small; 2000 would not be"
        raw = OutputXover(h_freq=idx).encode()
        assert raw[0] == idx
        assert raw[1] == 0  # high byte unused for an index this small


class TestOutputDynamics:
    def test_byte_layout(self):
        raw = OutputDynamics(
            all_pass_q=0x0102,
            attack_time=0x0304,
            release_time=0x0506,
            threshold=7,
            linkgroup_num=8,
        ).encode()
        assert raw == bytes([0x02, 0x01, 0x04, 0x03, 0x06, 0x05, 7, 8])

    def test_round_trip(self):
        v = OutputDynamics(all_pass_q=70, attack_time=10, release_time=100)
        assert OutputDynamics.decode(v.encode()) == v


class TestFrameBuilders:
    def test_eq_frame_addresses_the_band_by_data_id(self):
        f = write_output_eq(3, 17, EqBand(freq=20, level=100, bw=14))
        assert f.data_type == DataType.OUTPUT_CHANNEL
        assert f.channel_id == 3
        assert f.data_id == 17
        assert f.frame_type == FrameType.WRITE

    @pytest.mark.parametrize("band", [-1, OUTPUT_EQ_BANDS, 99])
    def test_eq_band_index_is_bounds_checked(self, band):
        with pytest.raises(ValueError, match="band must be"):
            write_output_eq(0, band, EqBand(freq=0, level=0, bw=0))

    def test_eq_band_indices_do_not_collide_with_blocks(self):
        # DataID 0..30 are EQ bands; 31+ are parameter blocks. An off-by-one
        # here would write an EQ band into the misc block.
        assert int(OutputBlock.MISC) == OUTPUT_EQ_BANDS

    def test_misc_frame_uses_data_id_31(self):
        assert write_output_misc(0, OutputMisc()).data_id == 31

    def test_xover_frame_uses_data_id_32(self):
        assert write_output_xover(0, OutputXover()).data_id == 32

    def test_dynamics_frame_uses_data_id_35(self):
        assert write_output_dynamics(0, OutputDynamics()).data_id == 35

    def test_frames_round_trip_through_the_wire(self):
        f = write_output_misc(5, OutputMisc(gain_raw=1234, delay_raw=99))
        back = decode(f.encode())
        assert back == f
        assert OutputMisc.decode(back.payload).gain_raw == 1234


class TestMixMatrix:
    def test_splits_across_two_frames(self):
        frames = write_output_mix(2, list(range(16)))
        assert [f.data_id for f in frames] == [
            OutputBlock.MIX_IN_1_8,
            OutputBlock.MIX_IN_9_16,
        ]
        assert frames[0].payload == bytes(range(8))
        assert frames[1].payload == bytes(range(8, 16))

    def test_short_vector_is_zero_padded(self):
        frames = write_output_mix(0, [99, 99])
        assert frames[0].payload == bytes([99, 99, 0, 0, 0, 0, 0, 0])
        assert frames[1].payload == bytes(8)

    def test_rejects_oversized_vector(self):
        with pytest.raises(ValueError, match="at most 16"):
            write_output_mix(0, list(range(17)))


class TestReadFrames:
    def test_read_carries_no_payload(self):
        f = read_output(4, OutputBlock.XOVER)
        assert f.frame_type == FrameType.READ
        assert f.payload == b""
        assert f.data_id == 32

    def test_read_frame_round_trips(self):
        f = read_output(1, OutputBlock.MISC)
        assert decode(f.encode()) == f


class TestSafety:
    def test_output_channel_96_is_not_destructive(self):
        # RESET_MCU is ChannelID 96 on DataType 9 only. The same ChannelID on
        # an output must stay usable.
        f = write_output_misc(96, OutputMisc())
        assert f.destructive is None
        f.encode()
