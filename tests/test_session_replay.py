"""Our connect ritual against the vendor app's, byte for byte.

The strongest check available short of hardware. :class:`ReplayTransport`
answers from the recorded session and refuses to answer anything the recording
did not contain, so driving ``handshake()`` through it asserts that **our first
31 requests are byte-identical to the app's** -- not similar, not equivalent,
identical.

That matters because a device is entitled to care about anything in those
bytes, and we cannot know which parts. Reproducing them exactly means the
question never has to be asked.

.. note::
   **Fields frozen by this oracle, and why.** The comparison is on raw bytes,
   so it pins every header field the app happened to use -- above all
   ``bluetooth_device_id = 4``, which is almost certainly an index into *that
   phone's* paired-device list rather than anything about the DSP.

   So a capture taken from a re-paired or different phone will show a different
   value and these tests will fail on bytes 10 and the checksum. That is not a
   regression in the session layer. Re-derive ``OBSERVED_BLUETOOTH_DEVICE_ID``
   from the new capture, or parameterise the fixture. Written down here so the
   failure is understood in seconds rather than re-derived.

   Also frozen: ``device_id = 1``, ``pc_custom = 0``, and the order of the
   ritual itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tuner.dsp.btsnoop import captured_frames
from tuner.dsp.session import (
    HANDSHAKE_SYSTEM_CHANNELS,
    Dsp408Session,
    Pacing,
)
from tuner.dsp.transport import ReplayTransport, TransportError

CAPTURE = Path(__file__).resolve().parents[1] / "captures" / "btsnoop_hci.log"

pytestmark = pytest.mark.skipif(not CAPTURE.exists(), reason="capture not present")

#: The connect ritual: 8 system reads, 15 preset names, 8 bulk channel reads.
HANDSHAKE_LEN = 31


@pytest.fixture(scope="module")
def exchanges() -> list[tuple[bytes, bytes]]:
    """Captured (request, reply) byte pairs, in order."""
    frames = captured_frames(CAPTURE.read_bytes())
    pairs, i = [], 0
    while i < len(frames) - 1:
        a, b = frames[i], frames[i + 1]
        if not a.received and b.received:
            pairs.append((a.raw, b.raw))
            i += 2
        else:
            i += 1
    assert len(pairs) == 2916
    return pairs


def _no_wait() -> Pacing:
    """Pacing that does not sleep; timing is tested in test_session.py."""
    return Pacing(idle_after_reply_s=0.0, max_requests_per_s=1e9)


class TestHandshakeIsIndistinguishable:
    def test_our_first_31_requests_match_the_apps_byte_for_byte(self, exchanges):
        transport = ReplayTransport(exchanges[:HANDSHAKE_LEN])
        session = Dsp408Session(transport, pacing=_no_wait())
        session.open()

        identity = session.handshake()

        assert transport.consumed == HANDSHAKE_LEN
        assert transport.remaining == 0
        assert session.stats.requests == HANDSHAKE_LEN
        assert session.stats.clean

        # And we parsed the real device's answers correctly.
        assert identity.firmware == "MYDW-AV1.06"
        assert identity.current_preset == 4
        assert identity.preset_names == (
            "re-timed",
            "rockkkkkk",
            "- bass",
            "lbass",
            "test",
            "basssss++++",
            "lbass",
            "lbass",
            "lbass",
            "lbass",
            "lbass",
            "lbass",
            "lbass",
            "lbass",
            "lbass",
        )
        assert len(identity.channels) == 8
        assert all(len(r) == 296 for r in identity.channels)

    def test_the_real_link_group_is_recovered(self, exchanges):
        # Outputs 7 and 8 are the only linked pair in the real device, which
        # is what explained them moving together in the vendor app.
        transport = ReplayTransport(exchanges[:HANDSHAKE_LEN])
        session = Dsp408Session(transport, pacing=_no_wait())
        session.open()
        identity = session.handshake()
        assert identity.linked_channels() == {6, 7}

    def test_the_policy_ends_up_refusing_those_channels(self, exchanges):
        transport = ReplayTransport(exchanges[:HANDSHAKE_LEN])
        session = Dsp408Session(transport, pacing=_no_wait())
        session.open()
        session.handshake()
        assert session.policy._linked == {6, 7}

    def test_the_unknown_system_blocks_are_carried_through_verbatim(self, exchanges):
        # We do not know what channels 2, 5, 6, 19 mean. Recording their bytes
        # without interpreting them is the honest option, and a snapshot needs
        # them anyway.
        transport = ReplayTransport(exchanges[:HANDSHAKE_LEN])
        session = Dsp408Session(transport, pacing=_no_wait())
        session.open()
        identity = session.handshake()
        assert set(identity.system_blocks) == set(HANDSHAKE_SYSTEM_CHANNELS)
        assert identity.system_blocks[19] == bytes.fromhex("83838383d1c7bbbbbbbb")
        assert identity.system_blocks[7] == b""
        assert identity.system_blocks[8] == b""


class TestTheOracleIsStrict:
    """A replay oracle that accepts anything proves nothing."""

    def test_a_wrong_request_is_rejected(self, exchanges):
        # Poll status instead of reading firmware: same shape, different
        # channel, so only a byte comparison catches it.
        transport = ReplayTransport(exchanges[:HANDSHAKE_LEN])
        session = Dsp408Session(transport, pacing=_no_wait())
        session.open()
        with pytest.raises(TransportError, match="do not match the recording"):
            session.poll_status()

    def test_a_wrong_link_id_is_rejected(self, exchanges):
        # The concrete reason bluetooth_device_id is stamped rather than left
        # at Frame's default of 0: the bytes differ, and so does the checksum.
        transport = ReplayTransport(exchanges[:HANDSHAKE_LEN])
        session = Dsp408Session(transport, pacing=_no_wait(), bluetooth_device_id=0)
        session.open()
        with pytest.raises(TransportError, match="do not match the recording"):
            session.handshake()

    def test_running_past_the_end_of_the_recording_is_rejected(self, exchanges):
        transport = ReplayTransport(exchanges[:2])
        session = Dsp408Session(transport, pacing=_no_wait())
        session.open()
        session.read_system(4)
        session.read_system(19)
        with pytest.raises(TransportError, match="past the end"):
            session.read_system(2)

    def test_chunked_writes_are_matched_after_reassembly(self, exchanges):
        # The session sends 20-byte chunks; the recording holds whole frames.
        # The oracle must compare frames, not chunks -- chunking is checked
        # separately against the raw stream in test_framing_replay.py.
        transport = ReplayTransport(exchanges[:1])
        session = Dsp408Session(transport, pacing=_no_wait())
        session.open()
        assert session.read_system(4) == b"MYDW-AV1.06"


@pytest.fixture(scope="module")
def write_exchanges(exchanges):
    """The 21 real parameter writes and their acks."""
    from tuner.dsp.protocol import FrameType, decode

    got = [
        (req, rep)
        for req, rep in exchanges
        if int(decode(req).frame_type) == FrameType.WRITE and decode(req).payload
    ]
    assert len(got) == 21
    return got


class TestCapturedWritesReplay:
    """The 21 real parameter writes, driven back through the session."""

    def test_every_captured_write_reproduces_byte_for_byte(self, write_exchanges):
        from tuner.dsp.protocol import decode
        from tuner.dsp.txpolicy import BlastRadius, TxPolicy

        transport = ReplayTransport(write_exchanges)
        session = Dsp408Session(
            transport,
            pacing=_no_wait(),
            policy=TxPolicy(
                allow_writes=True,
                blast_radius=BlastRadius(max_writes=99, max_channels=8),
            ),
        )
        session.open()

        for request, _ in write_exchanges:
            frame = decode(request)
            session.write_block(
                int(frame.channel_id), int(frame.data_id), frame.payload
            )

        assert transport.consumed == 21
        assert session.stats.clean

    def test_the_acks_are_all_bare(self, write_exchanges):
        from tuner.dsp.protocol import Ack, decode

        for _, reply in write_exchanges:
            frame = decode(reply)
            assert int(frame.frame_type) == int(Ack.RIGHT)
            assert frame.payload == b""
