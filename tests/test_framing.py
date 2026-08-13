"""Byte-stream framing, exercised deliberately rather than by luck.

The reference capture is clean -- zero malformed frames, zero checksum
failures, zero retransmissions across 5834 frames -- which makes it a good
oracle for *correct* input and no test at all for anything else. A real link
degrades; these build the awkward cases on purpose.

The property that matters most is in :class:`TestArbitrarySplits`: **the frames
recovered must not depend on how the byte stream was cut up.** Everything else
here is a way for that to fail.
"""

from __future__ import annotations

import random

import pytest

from tuner.dsp.framing import (
    CHUNK,
    MAX_PAYLOAD,
    PREAMBLE,
    FrameReader,
    chunk,
    chunk_20,
)
from tuner.dsp.protocol import DataType, Frame, FrameType


def _write(channel: int = 0, data_id: int = 31, payload: bytes | None = None) -> bytes:
    return Frame(
        frame_type=FrameType.WRITE,
        data_type=DataType.OUTPUT_CHANNEL,
        channel_id=channel,
        data_id=data_id,
        payload=payload if payload is not None else bytes(range(8)),
        bluetooth_device_id=4,
    ).encode()


def _read(channel: int = 3) -> bytes:
    return Frame(
        frame_type=FrameType.READ,
        data_type=DataType.SYSTEM,
        channel_id=channel,
        bluetooth_device_id=4,
    ).encode()


def _bulk(channel: int = 0) -> bytes:
    """A 296-byte whole-channel reply: 312 bytes, 16 chunks, 8 pad bytes."""
    return Frame(
        frame_type=0x53,
        data_type=DataType.OUTPUT_CHANNEL,
        channel_id=channel,
        data_id=119,
        payload=bytes((i * 7 + channel) & 0xFF for i in range(296)),
        bluetooth_device_id=4,
    ).encode()


def _feed_all(reader: FrameReader, stream: bytes, size: int) -> list[bytes]:
    out = []
    for i in range(0, len(stream), size):
        out += [f.raw for f in reader.feed(stream[i : i + size])]
    return out


class TestWholeFrames:
    def test_one_frame_arriving_whole(self):
        raw = _write()
        (got,) = FrameReader().feed(raw)
        assert got.raw == raw
        assert got.frame.data_id == 31

    def test_several_frames_in_one_feed(self):
        stream = _write(data_id=1) + _read() + _write(data_id=2)
        got = FrameReader().feed(stream)
        assert [f.raw for f in got] == [_write(data_id=1), _read(), _write(data_id=2)]

    def test_nothing_is_returned_until_a_frame_completes(self):
        raw = _write()
        reader = FrameReader()
        assert reader.feed(raw[:-1]) == []
        assert reader.pending == len(raw) - 1
        (got,) = reader.feed(raw[-1:])
        assert got.raw == raw
        assert reader.pending == 0

    def test_empty_feed_is_harmless(self):
        reader = FrameReader()
        assert reader.feed(b"") == []
        assert reader.pending == 0


class TestArbitrarySplits:
    """Recovered frames must not depend on how the stream was cut."""

    STREAM = _write(data_id=1) + _read() + _bulk() + _write(data_id=2)

    @pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 20, 64, 297, 4096])
    def test_fixed_size_splits_all_agree(self, size):
        assert _feed_all(FrameReader(), self.STREAM, size) == [
            _write(data_id=1),
            _read(),
            _bulk(),
            _write(data_id=2),
        ]

    @pytest.mark.parametrize("seed", range(8))
    def test_random_splits_all_agree(self, seed):
        rng = random.Random(seed)
        reader, out, i = FrameReader(), [], 0
        while i < len(self.STREAM):
            n = rng.randint(1, 40)
            out += [f.raw for f in reader.feed(self.STREAM[i : i + n])]
            i += n
        assert b"".join(out) == self.STREAM
        assert reader.pending == 0

    def test_a_preamble_split_across_feeds_is_not_lost(self):
        # The reader discards unrecognised bytes, so it must retain the last
        # three in case they are the start of a preamble.
        raw = _write()
        reader = FrameReader()
        assert reader.feed(raw[:2]) == []
        (got,) = reader.feed(raw[2:])
        assert got.raw == raw


class TestPadding:
    def test_zero_padding_between_frames_is_consumed(self):
        stream = _write() + bytes(16) + _read() + bytes(4)
        reader = FrameReader()
        got = reader.feed(stream)
        assert [f.raw for f in got] == [_write(), _read()]
        assert reader.stats.pad_bytes == 20
        assert reader.stats.clean

    def test_a_realistic_chunked_stream(self):
        # What the wire actually looks like: every frame padded to 20 bytes.
        frames = [_write(data_id=1), _read(), _bulk(), _write(data_id=2)]
        stream = b"".join(b"".join(chunk_20(f)) for f in frames)
        assert len(stream) % CHUNK == 0
        reader = FrameReader()
        assert [f.raw for f in reader.feed(stream)] == frames
        assert reader.stats.clean
        assert reader.stats.pad_bytes == len(stream) - sum(len(f) for f in frames)

    def test_zeros_inside_a_payload_are_not_stripped(self):
        # The pad-stripping rule is "leading zeros only". A payload of all
        # zeros must survive it intact.
        raw = _write(payload=bytes(8))
        (got,) = FrameReader().feed(raw)
        assert got.frame.payload == bytes(8)

    def test_leading_padding_before_any_frame(self):
        reader = FrameReader()
        (got,) = reader.feed(bytes(40) + _read())
        assert got.raw == _read()
        assert reader.stats.pad_bytes == 40
        assert reader.stats.resyncs == 0


class TestResync:
    def test_garbage_before_a_frame_is_skipped_and_counted(self):
        reader = FrameReader()
        (got,) = reader.feed(b"\x11\x22\x33" + _write())
        assert got.raw == _write()
        assert reader.stats.garbage_bytes == 3
        assert reader.stats.resyncs == 1

    def test_a_frame_after_a_corrupted_one_is_still_found(self):
        # The point of resyncing at all: one bad frame must not cost the rest
        # of the session.
        bad = bytearray(_write(data_id=1))
        bad[-2] ^= 0xFF  # break the checksum
        reader = FrameReader()
        got = reader.feed(bytes(bad) + _write(data_id=2))
        assert [f.raw for f in got] == [_write(data_id=2)]
        assert reader.stats.checksum_errors >= 1

    def test_a_chance_preamble_inside_a_payload_does_not_derail_the_parse(self):
        # 80 80 80 EE can occur in ordinary data. The frame carrying it must
        # decode normally, and the bytes must not be mistaken for a new frame.
        raw = _write(payload=PREAMBLE + bytes(4))
        reader = FrameReader()
        (got,) = reader.feed(raw)
        assert got.raw == raw
        assert reader.stats.clean

    def test_a_bare_chance_preamble_between_frames_is_discarded(self):
        reader = FrameReader()
        got = reader.feed(PREAMBLE + b"\x11\x22" + _read())
        assert [f.raw for f in got] == [_read()]
        assert reader.stats.resyncs >= 1

    def test_a_lone_preamble_is_held_not_dropped(self):
        # It may yet be the start of a real frame.
        reader = FrameReader()
        assert reader.feed(PREAMBLE) == []
        assert reader.pending == len(PREAMBLE)

    def test_trailing_garbage_is_bounded(self):
        reader = FrameReader()
        reader.feed(_read() + b"\x11" * 500)
        assert reader.pending <= len(PREAMBLE) - 1


class TestOversizeLength:
    """The bound that stops a corrupted length field hanging the reader."""

    def test_an_absurd_length_does_not_stall_the_reader(self):
        # Without MAX_PAYLOAD this waits for 65535 bytes that never come, and
        # the symptom is indistinguishable from a dead link.
        poisoned = bytearray(_write())
        poisoned[12], poisoned[13] = 0xFF, 0xFF
        reader = FrameReader()
        got = reader.feed(bytes(poisoned) + _read())
        assert [f.raw for f in got] == [_read()]
        assert reader.stats.oversize_length >= 1
        assert reader.pending == 0

    def test_the_largest_real_payload_is_inside_the_bound(self):
        # 296 is the whole-channel readback; the bound must not reject it.
        assert MAX_PAYLOAD >= 296
        (got,) = FrameReader().feed(_bulk())
        assert len(got.frame.payload) == 296

    def test_a_length_just_over_the_bound_is_rejected(self):
        poisoned = bytearray(_read())
        n = MAX_PAYLOAD + 1
        poisoned[12], poisoned[13] = n & 0xFF, (n >> 8) & 0xFF
        reader = FrameReader()
        assert reader.feed(bytes(poisoned)) == []
        assert reader.stats.oversize_length >= 1


class TestReaderHousekeeping:
    def test_reset_discards_a_partial_frame(self):
        reader = FrameReader()
        reader.feed(_write()[:-3])
        assert reader.pending > 0
        reader.reset()
        assert reader.pending == 0

    def test_stats_clean_is_false_after_trouble(self):
        reader = FrameReader()
        reader.feed(b"\x11\x22" + _read())
        assert not reader.stats.clean

    def test_feed_never_raises_on_arbitrary_bytes(self):
        # A reader that throws mid-session strands the transaction layer with
        # no way to recover, so malformed input is counted, not raised.
        rng = random.Random(1234)
        reader = FrameReader()
        for _ in range(200):
            reader.feed(bytes(rng.randrange(256) for _ in range(rng.randint(0, 64))))


class TestChunking:
    def test_a_read_frame_is_one_chunk_with_four_pad_bytes(self):
        chunks = chunk_20(_read())
        assert len(chunks) == 1
        assert len(chunks[0]) == CHUNK
        assert chunks[0][16:] == bytes(4)

    def test_a_write_frame_spans_two_chunks(self):
        raw = _write()
        assert len(raw) == 24
        chunks = chunk_20(raw)
        assert len(chunks) == 2
        assert b"".join(chunks)[: len(raw)] == raw
        assert b"".join(chunks)[len(raw) :] == bytes(16)

    def test_a_bulk_reply_spans_sixteen_chunks(self):
        raw = _bulk()
        assert len(raw) == 312
        chunks = chunk_20(raw)
        assert len(chunks) == 16
        assert b"".join(chunks)[len(raw) :] == bytes(8)

    @pytest.mark.parametrize("size", [16, 24, 40, 312])
    def test_every_chunk_is_exactly_the_link_size(self, size):
        raw = _write(payload=bytes(size - 16))
        assert {len(c) for c in chunk_20(raw)} == {CHUNK}

    def test_padding_can_be_disabled(self):
        # The untested alternative -- kept reachable so it can be tried on
        # something other than the only unit.
        raw = _write()
        assert chunk(raw, pad_to=None) == [raw]

    def test_chunks_round_trip_through_the_reader(self):
        raw = _bulk()
        reader = FrameReader()
        out = []
        for piece in chunk_20(raw):
            out += reader.feed(piece)
        assert [f.raw for f in out] == [raw]

    def test_rejects_nonsense_arguments(self):
        with pytest.raises(ValueError, match="nothing to send"):
            chunk(b"")
        with pytest.raises(ValueError, match="positive"):
            chunk(_read(), pad_to=0)
