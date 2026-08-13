"""Framing replayed against real device traffic.

`test_framing.py` proves the reader handles the awkward cases we invented.
These prove it handles **the bytes a real DSP-408 and the vendor app actually
exchanged**, which is the only thing that settles whether our idea of the
transport matches the device's.

Two oracles, both byte-exact:

* **Reader** -- feed the captured stream through :class:`FrameReader` in
  randomized chunk sizes and require exactly the frames ``btsnoop`` finds, in
  order, with no losses and no manufactured extras.
* **Chunker** -- take each captured host frame, re-encode it, run
  :func:`chunk_20`, and require the result to equal the corresponding slice of
  the real host byte stream. That is a byte-exact test of the 20-byte padding
  rule against ground truth, not merely a test that our own two functions
  agree with each other.

The reader oracle shares ``decode()`` with ``btsnoop._scan``, so it tests
*framing*, not the codec -- the codec is already pinned by
``test_golden_frames.py``. What it adds is that framing survives arbitrary
stream fragmentation, which ``_scan`` never has to face because it works on a
fully assembled buffer.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from tuner.dsp.btsnoop import DSP408_DLCI, _scan, packets, rfcomm_streams
from tuner.dsp.framing import CHUNK, FrameReader, chunk_20
from tuner.dsp.protocol import decode

CAPTURE = Path(__file__).resolve().parents[1] / "captures" / "btsnoop_hci.log"

pytestmark = pytest.mark.skipif(not CAPTURE.exists(), reason="capture not present")

#: Measured totals. Hard-coded so a change in the reader that silently loses
#: frames fails loudly rather than quietly agreeing with a recomputed number.
HOST_FRAMES = 2918
DEVICE_FRAMES = 2916


@pytest.fixture(scope="module")
def streams() -> dict[bool, bytes]:
    pkts = packets(CAPTURE.read_bytes())
    got = rfcomm_streams(pkts, DSP408_DLCI)
    return {recv: buf for recv, (buf, _) in got.items()}


@pytest.fixture(scope="module")
def scanned(streams) -> dict[bool, list[tuple[int, bytes]]]:
    return {recv: _scan(buf) for recv, buf in streams.items()}


class TestStreamShape:
    """What the DLCI-2 streams look like before any parsing."""

    def test_totals(self, streams, scanned):
        assert len(scanned[False]) == HOST_FRAMES
        assert len(scanned[True]) == DEVICE_FRAMES

    def test_both_streams_are_a_whole_number_of_link_chunks(self, streams):
        for recv, buf in streams.items():
            assert len(buf) % CHUNK == 0, recv

    def test_every_frame_starts_on_a_chunk_boundary(self, scanned):
        for recv, found in scanned.items():
            offsets = [o for o, _ in found]
            assert all(o % CHUNK == 0 for o in offsets), recv

    def test_every_inter_frame_gap_is_zero_padding(self, streams, scanned):
        # The claim the reader's pad-stripping depends on. If gaps ever held
        # anything else, stripping them would be discarding data.
        for recv, found in scanned.items():
            stream = streams[recv]
            for (off, raw), (nxt, _) in zip(found, found[1:], strict=False):
                gap = stream[off + len(raw) : nxt]
                assert set(gap) <= {0}, f"{recv} gap at {off + len(raw)}"


class TestReaderOracle:
    @pytest.mark.parametrize("received", [False, True])
    def test_whole_stream_in_one_feed(self, streams, scanned, received):
        reader = FrameReader()
        got = reader.feed(streams[received])
        assert [f.raw for f in got] == [raw for _, raw in scanned[received]]
        assert reader.pending == 0

    @pytest.mark.parametrize("received", [False, True])
    @pytest.mark.parametrize("size", [1, 7, 20, 120, 1024])
    def test_fixed_size_fragmentation(self, streams, scanned, received, size):
        stream = streams[received]
        reader = FrameReader()
        got = []
        for i in range(0, len(stream), size):
            got += reader.feed(stream[i : i + size])
        assert [f.raw for f in got] == [raw for _, raw in scanned[received]]

    @pytest.mark.parametrize("seed", [0, 1, 2])
    def test_random_fragmentation(self, streams, scanned, seed):
        stream = streams[True]
        rng = random.Random(seed)
        reader, got, i = FrameReader(), [], 0
        while i < len(stream):
            n = rng.randint(1, 200)
            got += reader.feed(stream[i : i + n])
            i += n
        assert [f.raw for f in got] == [raw for _, raw in scanned[True]]

    def test_the_reader_reports_a_clean_stream(self, streams):
        # No garbage, no resyncs, no checksum failures, no oversize lengths.
        # If a future change starts silently resyncing past real data, this is
        # what catches it.
        for received in (False, True):
            reader = FrameReader()
            reader.feed(streams[received])
            assert reader.stats.clean, f"{received}: {reader.stats}"
            assert reader.stats.frames == (DEVICE_FRAMES if received else HOST_FRAMES)

    def test_padding_accounts_for_every_non_frame_byte(self, streams, scanned):
        for received in (False, True):
            reader = FrameReader()
            reader.feed(streams[received])
            frame_bytes = sum(len(raw) for _, raw in scanned[received])
            assert reader.stats.pad_bytes == len(streams[received]) - frame_bytes

    def test_the_bulk_replies_survive_fragmentation(self, streams, scanned):
        # 296-byte payloads: 312-byte frames spanning 16 link chunks. The case
        # most likely to break partial buffering, and there are only eight of
        # them in 2916 frames -- too few to rely on hitting by chance.
        bulk = [
            raw
            for _, raw in scanned[True]
            if decode(raw).data_id == 119 and len(decode(raw).payload) == 296
        ]
        assert len(bulk) == 8, "capture should hold one bulk reply per channel"

        reader = FrameReader()
        got = []
        stream = streams[True]
        for i in range(0, len(stream), 3):  # deliberately not a chunk divisor
            got += reader.feed(stream[i : i + 3])
        recovered = [
            f.raw for f in got if f.frame.data_id == 119 and len(f.frame.payload) == 296
        ]
        assert recovered == bulk


class TestChunkerOracle:
    """Our padding rule against the bytes the app actually put on the wire."""

    def test_every_host_frame_rechunks_to_the_real_stream(self, streams, scanned):
        stream = streams[False]
        for offset, raw in scanned[False]:
            rebuilt = b"".join(chunk_20(decode(raw).encode()))
            assert rebuilt == stream[offset : offset + len(rebuilt)], (
                f"frame at {offset} does not re-chunk to the captured bytes"
            )

    def test_the_whole_host_stream_is_reproduced_end_to_end(self, streams, scanned):
        # Stronger than the per-frame check: rebuild the entire 58 780-byte
        # stream from decoded frames alone. Any drift in padding, ordering or
        # encoding shows up as a length or content mismatch.
        rebuilt = b"".join(
            b"".join(chunk_20(decode(raw).encode())) for _, raw in scanned[False]
        )
        assert rebuilt == streams[False]

    def test_chunk_counts_match_the_frame_sizes_seen(self, scanned):
        counts = {}
        for _, raw in scanned[False]:
            counts.setdefault(len(raw), set()).add(len(chunk_20(raw)))
        # Reads are 16 bytes (1 chunk), block writes 24 (2 chunks).
        assert counts == {16: {1}, 24: {2}}
