"""The transmit allow-list, validated against real device traffic.

The central test is ``test_every_captured_host_frame_is_permitted``.
An allow-list that refuses something the vendor app does routinely is not
cautious, it is broken -- it would block the connect ritual and we would loosen
it under pressure, in a hurry, on the only unit. So the list is derived from
the capture and checked against all 2918 host frames.

The converse tests matter just as much: a policy that permits everything passes
that check too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tuner.dsp.btsnoop import captured_frames
from tuner.dsp.protocol import (
    DESTRUCTIVE_COMMANDS,
    DataType,
    Frame,
    FrameType,
    OutputBlock,
)
from tuner.dsp.txpolicy import (
    SYSTEM_READ_CHANNELS,
    BlastRadius,
    TxPolicy,
    TxRefused,
)

CAPTURE = Path(__file__).resolve().parents[1] / "captures" / "btsnoop_hci.log"


def _write(channel: int = 0, data_id: int = 31, payload: bytes = bytes(8)) -> Frame:
    return Frame(
        frame_type=FrameType.WRITE,
        data_type=DataType.OUTPUT_CHANNEL,
        channel_id=channel,
        data_id=data_id,
        payload=payload,
    )


def _read(
    data_type: int = DataType.SYSTEM, channel: int = 3, data_id: int = 0
) -> Frame:
    return Frame(
        frame_type=FrameType.READ,
        data_type=data_type,
        channel_id=channel,
        data_id=data_id,
    )


def _armed(**kw) -> TxPolicy:
    kw.setdefault("allow_writes", True)
    kw.setdefault("blast_radius", BlastRadius(max_writes=1000, max_channels=8))
    return TxPolicy(**kw)


@pytest.fixture(scope="module")
def host_frames():
    if not CAPTURE.exists():
        pytest.skip("capture not present")
    got = [f.frame for f in captured_frames(CAPTURE.read_bytes()) if not f.received]
    assert len(got) == 2918
    return got


class TestAgainstTheCapture:
    def test_every_captured_host_frame_is_permitted(self, host_frames):
        # The whole point. A rule that blocks real app traffic is a rule we
        # would end up disabling at the worst possible moment.
        policy = _armed()
        for i, frame in enumerate(host_frames):
            try:
                policy.check(frame)
            except TxRefused as exc:  # pragma: no cover - failure path
                pytest.fail(
                    f"frame {i} (ft=0x{int(frame.frame_type):02X} "
                    f"dt={frame.data_type} ch={frame.channel_id} "
                    f"did={frame.data_id}) refused: {exc}"
                )

    def test_reads_alone_need_no_write_permission(self, host_frames):
        # A read-only session -- bring-up stages 1 and 2 -- must work with
        # writes disabled, which is what makes "read-only" a real guarantee
        # rather than a promise about how we intend to call it.
        policy = TxPolicy()
        reads = [f for f in host_frames if int(f.frame_type) == FrameType.READ]
        assert len(reads) == 2897
        for frame in reads:
            policy.check(frame)

    def test_the_captured_writes_are_refused_without_arming(self, host_frames):
        policy = TxPolicy()
        writes = [f for f in host_frames if int(f.frame_type) == FrameType.WRITE]
        assert len(writes) == 21
        for frame in writes:
            with pytest.raises(TxRefused, match="not enabled"):
                policy.check(frame)

    def test_the_observed_tuples_are_what_the_list_covers(self, host_frames):
        # The entire evidential basis for the allow-list, written out rather
        # than counted, so that widening it later is visibly a decision.
        # 22 distinct (frame_type, data_type, channel_id, data_id) tuples --
        # 36 if user_id is included, but user_id only indexes the 15 preset
        # name slots and is not part of what the policy authorises.
        seen = {
            (int(f.frame_type), int(f.data_type), int(f.channel_id), int(f.data_id))
            for f in host_frames
        }
        writes = {(0xA1, 4, 0, did) for did in (2, 3, 31, 32)}
        bulk_reads = {(0xA2, 4, ch, 119) for ch in range(8)}
        system_reads = {(0xA2, 9, ch, 0) for ch in SYSTEM_READ_CHANNELS}
        assert seen == writes | bulk_reads | system_reads
        assert len(seen) == 22

    def test_the_allow_list_is_wider_than_the_evidence_only_where_argued(self):
        # Two deliberate extrapolations, both recorded rather than assumed:
        #   - writes to channels 1-7 (every observed write went to channel 0)
        #   - writes to EQ bands and blocks the app did not happen to touch
        # Anything beyond those is a bug in the list, not a judgement call.
        policy = _armed()
        policy.check(_write(channel=5, data_id=20))  # permitted by extrapolation
        with pytest.raises(TxRefused):
            policy.check(_write(channel=8, data_id=20))  # not a real output


class TestRefusals:
    def test_destructive_opcodes_are_refused(self):
        for (data_type, channel), name in DESTRUCTIVE_COMMANDS.items():
            frame = Frame(
                frame_type=FrameType.READ, data_type=data_type, channel_id=channel
            )
            with pytest.raises(TxRefused, match="no observed system read"):
                _armed().check(frame), name

    def test_a_destructive_write_is_refused_by_both_layers(self):
        # Defence in depth: the policy refuses it, and protocol.encode would
        # too. Two independent rules have to fail for it to reach the wire.
        frame = Frame(
            frame_type=FrameType.WRITE,
            data_type=DataType.SYSTEM,
            channel_id=96,
            payload=bytes(8),
        )
        with pytest.raises(TxRefused, match="only writes ever observed"):
            _armed().check(frame)
        from tuner.dsp.protocol import DestructiveCommand

        with pytest.raises(DestructiveCommand):
            frame.encode()

    @pytest.mark.parametrize("data_id", [34, 35])
    def test_the_contradicted_blocks_may_not_be_written(self, data_id):
        with pytest.raises(TxRefused, match="contradicted"):
            _armed().check(_write(data_id=data_id))

    def test_the_contradicted_blocks_may_still_be_read(self):
        # Reading them is how the contradiction gets settled, and reads are
        # harmless.
        _armed().check(_read(DataType.OUTPUT_CHANNEL, channel=0, data_id=34))

    def test_the_bulk_aggregate_may_not_be_written(self):
        with pytest.raises(TxRefused, match="bulk"):
            _armed().check(_write(data_id=119))

    def test_the_bulk_aggregate_may_be_read(self):
        _armed().check(_read(DataType.OUTPUT_CHANNEL, channel=5, data_id=119))

    def test_input_channels_are_refused_entirely(self):
        # DataType 3 never appears in the capture, in either direction.
        with pytest.raises(TxRefused, match="never observed"):
            _armed().check(_read(DataType.INPUT_CHANNEL, channel=0))

    def test_an_unobserved_system_channel_is_refused(self):
        with pytest.raises(TxRefused, match="no observed system read"):
            _armed().check(_read(DataType.SYSTEM, channel=37))

    @pytest.mark.parametrize("channel", [8, 9, 200, 255])
    def test_channels_outside_the_hardware_are_refused(self, channel):
        with pytest.raises(TxRefused, match="outside 0-7"):
            _armed().check(_write(channel=channel))

    def test_a_negative_channel_never_reaches_the_policy(self):
        # Frame's own field validation catches it first. Recorded so the
        # policy's 0-7 check is understood as the second line, not the only
        # one -- and so this stays true if Frame is ever relaxed.
        with pytest.raises(ValueError, match="does not fit in one byte"):
            _write(channel=-1)

    @pytest.mark.parametrize("size", [0, 1, 7, 9, 296])
    def test_a_write_must_carry_a_whole_block(self, size):
        # The read-modify-write rule, enforced rather than documented: every
        # observed write is 8 bytes, and a shorter one would revert whatever
        # it omitted.
        with pytest.raises(TxRefused, match="whole 8-byte block"):
            _armed().check(_write(payload=bytes(size)))

    def test_device_response_types_may_not_be_transmitted(self):
        frame = Frame(frame_type=0x53, data_type=DataType.SYSTEM, channel_id=3)
        with pytest.raises(TxRefused, match="device response"):
            _armed().check(frame)


class TestLinkedChannels:
    def test_a_linked_channel_is_refused(self):
        policy = _armed()
        policy.set_linked_channels({6, 7})
        with pytest.raises(TxRefused, match="link group"):
            policy.check(_write(channel=6))

    def test_unlinked_channels_are_unaffected(self):
        policy = _armed()
        policy.set_linked_channels({6, 7})
        policy.check(_write(channel=0))

    def test_the_refusal_can_be_lifted_once_it_is_measured(self):
        policy = _armed(refuse_linked_channels=False)
        policy.set_linked_channels({6, 7})
        policy.check(_write(channel=6))


class TestBlastRadius:
    def test_the_write_cap_stops_a_runaway_loop(self):
        policy = TxPolicy(
            allow_writes=True, blast_radius=BlastRadius(max_writes=3, max_channels=8)
        )
        for _ in range(3):
            frame = _write()
            policy.check(frame)
            policy.note_sent(frame)
        with pytest.raises(TxRefused, match="3 writes already sent"):
            policy.check(_write())

    def test_the_channel_cap_defaults_to_one(self):
        # Bring-up writes one channel at a time and verifies each by whole-
        # record readback before moving on.
        policy = TxPolicy(allow_writes=True)
        frame = _write(channel=0)
        policy.check(frame)
        policy.note_sent(frame)
        policy.check(_write(channel=0))  # same channel, still fine
        with pytest.raises(TxRefused, match="1 channel"):
            policy.check(_write(channel=1))

    def test_checking_does_not_spend_budget(self):
        # check() validates; note_sent() records what actually went out. A
        # caller that validates a plan must not exhaust its own cap doing so.
        policy = TxPolicy(
            allow_writes=True, blast_radius=BlastRadius(max_writes=1, max_channels=8)
        )
        for _ in range(10):
            policy.check(_write())
        assert policy.writes == 0

    def test_reads_never_count_against_the_caps(self):
        policy = TxPolicy(blast_radius=BlastRadius(max_writes=0, max_channels=0))
        for _ in range(50):
            frame = _read()
            policy.check(frame)
            policy.note_sent(frame)
        assert policy.writes == 0

    def test_negative_caps_are_rejected(self):
        with pytest.raises(ValueError, match="negative"):
            BlastRadius(max_writes=-1)


class TestCoverageOfTheAllowList:
    """The list should permit exactly the blocks we mean it to."""

    @pytest.mark.parametrize("data_id", list(range(0, 34)) + [36])
    def test_writable_ids(self, data_id):
        _armed().check(_write(data_id=data_id))

    @pytest.mark.parametrize("data_id", [34, 35, 37, 118, 119, 255])
    def test_non_writable_ids(self, data_id):
        with pytest.raises(TxRefused):
            _armed().check(_write(data_id=data_id))

    def test_the_name_block_is_writable(self):
        _armed().check(_write(data_id=int(OutputBlock.NAME)))

    @pytest.mark.parametrize("channel", sorted(SYSTEM_READ_CHANNELS))
    def test_every_documented_system_channel_is_readable(self, channel):
        _armed().check(_read(DataType.SYSTEM, channel=channel))


class TestPresetSlotAddressing:
    """``user_id`` on an output frame, and why a *read* had to become refusable.

    Measured 2026-08-09 from a second HCI capture. A preset recall in the
    vendor app is eight ``READ`` frames on ``OUTPUT_CHANNEL`` channels 0-7 at
    ``data_id`` 0 with ``user_id`` set to the slot. There is no select opcode
    and no write anywhere in the sequence -- the read itself loads the slot over
    the working area, all eight channels, on a device with no undo.

    The policy previously inspected ``user_id`` nowhere and listed 0 as a
    readable output block, so it permitted that frame. No caller set the field,
    so there was no live bug; what there was is the last line of defence
    holding the door open for the most destructive frame in the protocol while
    believing reads are inherently safe.
    """

    def test_a_preset_recall_read_is_refused(self):
        for slot in range(1, 7):
            frame = Frame(
                frame_type=FrameType.READ,
                data_type=DataType.OUTPUT_CHANNEL,
                channel_id=0,
                data_id=0,
                user_id=slot,
            )
            with pytest.raises(TxRefused, match="preset recall"):
                TxPolicy().check(frame)

    def test_the_refusal_says_what_the_frame_would_actually_do(self):
        # A message saying "not on the allow-list" would be true and useless.
        # Whoever hits this needs to know the frame wipes the tune.
        frame = Frame(
            frame_type=FrameType.READ,
            data_type=DataType.OUTPUT_CHANNEL,
            channel_id=0,
            data_id=0,
            user_id=3,
        )
        with pytest.raises(TxRefused) as excinfo:
            TxPolicy().check(frame)
        text = str(excinfo.value)
        assert "slot 3" in text
        assert "working area" in text
        assert "no undo" in text

    def test_any_output_read_addressing_a_slot_is_refused(self):
        # Not just data_id 0. Nothing is known about what a slot-addressed read
        # of an individual block does, and "unknown" is a refusal.
        for data_id in (0, 5, 31, 119):
            frame = Frame(
                frame_type=FrameType.READ,
                data_type=DataType.OUTPUT_CHANNEL,
                channel_id=2,
                data_id=data_id,
                user_id=6,
            )
            with pytest.raises(TxRefused, match="user_id=6"):
                TxPolicy().check(frame)

    def test_a_slot_addressed_write_is_refused(self):
        # The mirror image: user_id set on a write stores into the slot.
        frame = Frame(
            frame_type=FrameType.WRITE,
            data_type=DataType.OUTPUT_CHANNEL,
            channel_id=0,
            data_id=31,
            user_id=6,
            payload=bytes(8),
        )
        with pytest.raises(TxRefused, match="stored preset slot 6"):
            _armed().check(frame)

    def test_working_area_frames_still_pass(self):
        # The guard must not cost us ordinary traffic.
        TxPolicy().check(
            _read(data_type=DataType.OUTPUT_CHANNEL, channel=0, data_id=119)
        )
        _armed().check(_write())

    def test_preset_name_reads_may_still_carry_a_slot(self):
        # DataType 9 / ChannelID 0 is the preset-name namespace, where user_id
        # selecting a slot is both normal and non-destructive -- the app reads
        # all of them on connect. The refusal is specific to OUTPUT_CHANNEL.
        for slot in range(1, 7):
            TxPolicy().check(
                Frame(
                    frame_type=FrameType.READ,
                    data_type=DataType.SYSTEM,
                    channel_id=0,
                    data_id=0,
                    user_id=slot,
                )
            )

    def test_the_bulk_write_form_is_refused_by_length(self):
        # data_id 0 is overloaded: 8 bytes is EQ band 0, 296 bytes is a whole
        # channel record. On the wire only the length separates them, so the
        # block-length check is load-bearing rather than incidental.
        frame = Frame(
            frame_type=FrameType.WRITE,
            data_type=DataType.OUTPUT_CHANNEL,
            channel_id=0,
            data_id=0,
            payload=bytes(296),
        )
        with pytest.raises(TxRefused, match="whole 8-byte block"):
            _armed().check(frame)
