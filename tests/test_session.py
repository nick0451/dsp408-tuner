"""Transaction discipline, driven against the in-process fake.

Covers the rules the capture established -- lock-step, echo matching, reply
kinds, pacing, the connect ritual -- and the error path, which the capture
cannot cover because the device never once returned ``0x52``.

Timing is tested with an injected clock rather than by sleeping, so the suite
stays fast and the assertions are about the policy rather than about how
loaded the machine was.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tuner.dsp.fake_device import (
    DeviceImage,
    FakeDsp408,
    Faults,
    ProtocolViolationByHost,
    UnexpectedRequest,
)
from tuner.dsp.protocol import Ack, DataType, Frame, FrameType
from tuner.dsp.session import (
    OBSERVED_BLUETOOTH_DEVICE_ID,
    ConcurrentTransaction,
    DeviceRejected,
    Dsp408Session,
    FaultPolicy,
    IdleSurvival,
    LinkIdMismatch,
    Pacing,
    ProtocolViolation,
    ReplyTimeout,
    SessionPoisoned,
    SuspectedUsbArbitration,
    UnexpectedDevice,
    _echo_key,
)
from tuner.dsp.transport import LoopbackTransport, NotConnected, Transport
from tuner.dsp.txpolicy import BlastRadius, TxPolicy, TxRefused


class FakeClock:
    """Monotonic time that only advances when something sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def tick(self, seconds: float) -> None:
        self.now += seconds


def _session(device=None, **kw) -> tuple[Dsp408Session, FakeDsp408, FakeClock]:
    device = device or FakeDsp408()
    clock = FakeClock()
    kw.setdefault("clock", clock)
    kw.setdefault("sleep", clock.sleep)
    session = Dsp408Session(LoopbackTransport(device), **kw)
    session.open()
    return session, device, clock


def _armed_policy() -> TxPolicy:
    return TxPolicy(
        allow_writes=True, blast_radius=BlastRadius(max_writes=999, max_channels=8)
    )


class TestHandshake:
    def test_it_replays_the_captured_ritual_exactly(self):
        session, device, _ = _session()
        identity = session.handshake()

        # 8 system reads + 15 preset names + 8 bulk reads = 31, which is what
        # the vendor app did.
        assert session.stats.requests == 31
        assert len(device.received) == 31
        assert session.stats.clean

        # And in the observed order.
        order = [
            (int(f.data_type), int(f.channel_id), int(f.user_id))
            for f in device.received
        ]
        assert order[:8] == [(9, ch, 0) for ch in (4, 19, 2, 5, 6, 7, 8, 52)]
        assert order[8:23] == [(9, 0, slot) for slot in range(1, 16)]
        assert order[23:] == [(4, ch, 0) for ch in range(8)]

        assert identity.firmware == "MYDW-AV1.06"
        assert identity.current_preset == 4
        assert identity.preset_names[0] == "re-timed"
        assert identity.preset_names[3] == "lbass"
        assert len(identity.channels) == 8
        assert all(len(r) == 296 for r in identity.channels)

    def test_bulk_reads_use_data_id_119(self):
        session, device, _ = _session()
        session.handshake()
        bulk = [f for f in device.received if int(f.data_type) == 4]
        assert {int(f.data_id) for f in bulk} == {119}

    def test_it_refuses_an_unrecognised_device(self):
        # Set on the image, not on the module constant. `DeviceImage.system`
        # became per-instance on 2026-08-09, once channel 5 turned out to be
        # the master volume -- a global that can move is state, not a reply
        # table -- and monkey-patching a module dict no longer reaches an
        # already-constructed device. Better anyway: no global to restore.
        device = FakeDsp408()
        device.image.system[4] = b"SOMETHING-ELSE"
        session, _, _ = _session(device)
        with pytest.raises(UnexpectedDevice, match="MYDW-AV"):
            session.handshake()

    def test_it_teaches_the_policy_which_channels_are_linked(self):
        image = DeviceImage.flat()
        image.channels[6][280 + 7] = 1  # linkgroup_num on outputs 7 and 8
        image.channels[7][280 + 7] = 1
        policy = _armed_policy()
        session, _, _ = _session(FakeDsp408(image), policy=policy)

        identity = session.handshake()
        assert identity.linked_channels() == {6, 7}

        # And the policy now refuses them, without anyone wiring it up by hand.
        with pytest.raises(TxRefused, match="link group"):
            session.write_block(6, 31, bytes(8))
        session.write_block(0, 31, bytes(8))  # unlinked, still fine


class TestLockStep:
    def test_the_fake_rejects_pipelining(self):
        # Proves the fake can actually detect it, so the next test means
        # something.
        device = FakeDsp408()
        transport = LoopbackTransport(device)
        transport.open()
        raw = Frame(
            frame_type=FrameType.READ, data_type=DataType.SYSTEM, channel_id=3
        ).encode()
        transport.write(raw)
        with pytest.raises(ProtocolViolationByHost, match="lock-step"):
            transport.write(raw)

    def test_the_session_never_pipelines(self):
        session, device, _ = _session()
        session.handshake()
        for _ in range(20):
            session.poll_status()
        assert device.lock_step_violations == 0

    def test_a_second_concurrent_transaction_raises(self):
        # Raised, not serialised: a blocking lock would produce correct wire
        # behaviour while hiding a caller that thinks it can pipeline.
        session, _, _ = _session()
        session._lock.acquire()
        try:
            with pytest.raises(ConcurrentTransaction):
                session.poll_status()
        finally:
            session._lock.release()


class TestEchoMatching:
    def test_the_echo_key_excludes_frame_type_and_payload(self):
        request = Frame(
            frame_type=FrameType.READ, data_type=DataType.SYSTEM, channel_id=3
        )
        reply = Frame(
            frame_type=Ack.DATA,
            data_type=DataType.SYSTEM,
            channel_id=3,
            payload=b"\x01\x02",
        )
        assert _echo_key(request) == _echo_key(reply)

    def test_a_corrupted_echo_is_not_accepted_as_the_reply(self):
        # The bug this prevents: matching on arrival order would take this
        # reply as the answer to the outstanding request.
        session, device, clock = _session(
            FakeDsp408(faults=Faults(broken_echo=True)),
            pacing=Pacing(reply_timeout_s=0.05, idle_after_reply_s=0.0),
        )
        with pytest.raises(ReplyTimeout):
            session.poll_status()
        assert session.stats.stale_replies >= 1

    def test_stale_replies_beyond_the_bound_poison_the_session(self):
        session, _, _ = _session(
            FakeDsp408(faults=Faults(broken_echo=True)),
            pacing=Pacing(reply_timeout_s=0.05, idle_after_reply_s=0.0, retries=0),
            faults=FaultPolicy(max_stale_replies=0),
        )
        with pytest.raises(ProtocolViolation, match="out of step"):
            session.poll_status()
        assert session.poisoned


class TestReplyKinds:
    def test_a_read_is_answered_with_data(self):
        session, _, _ = _session()
        assert session.read_system(4) == b"MYDW-AV1.06"

    def test_a_write_is_answered_with_a_bare_ack(self):
        session, device, _ = _session(policy=_armed_policy())
        session.write_block(0, 31, bytes([1, 1, 0xF4, 1, 0, 0, 0, 1]))
        assert len(device.writes) == 1
        assert device.image.channels[0][248:256] == bytes([1, 1, 0xF4, 1, 0, 0, 0, 1])

    def test_an_error_reply_aborts_and_never_retries(self):
        # 0x52 was never observed, so its meaning is unknown. Retrying an
        # unknown error is how you learn what it meant the expensive way.
        session, _, _ = _session(FakeDsp408(faults=Faults(reply_error=True)))
        with pytest.raises(DeviceRejected, match="0x52"):
            session.poll_status()
        assert session.stats.retries == 0
        assert session.poisoned

    def test_a_poisoned_session_refuses_everything_afterwards(self):
        session, _, _ = _session(FakeDsp408(faults=Faults(reply_error=True)))
        with pytest.raises(DeviceRejected):
            session.poll_status()
        with pytest.raises(SessionPoisoned):
            session.poll_status()

    def test_poisoning_can_be_switched_off_for_diagnostics(self):
        session, device, _ = _session(
            FakeDsp408(faults=Faults(reply_error=True)),
            faults=FaultPolicy(poison_on_fault=False),
        )
        with pytest.raises(DeviceRejected):
            session.poll_status()
        assert session.poisoned is None
        device.faults.reply_error = False
        assert session.poll_status()


class TestTimeoutAndRetry:
    def test_one_dropped_reply_is_retried_with_identical_bytes(self):
        # Safe only because whole-block writes are idempotent: a write carries
        # all eight bytes and never a delta.
        device = FakeDsp408(faults=Faults(drop_replies=1))
        session, _, clock = _session(
            device, pacing=Pacing(reply_timeout_s=0.05, idle_after_reply_s=0.0)
        )
        assert session.poll_status()
        assert session.stats.retries == 1
        assert session.stats.timeouts == 1
        assert 3.2 in clock.slept  # the app's observed retry interval

    def test_persistent_silence_gives_up_and_poisons(self):
        session, _, _ = _session(
            FakeDsp408(faults=Faults(drop_replies=99)),
            pacing=Pacing(reply_timeout_s=0.02, idle_after_reply_s=0.0),
        )
        with pytest.raises(ReplyTimeout):
            session.poll_status()
        assert session.stats.timeouts == 2  # initial attempt plus one retry
        assert session.poisoned

    def test_retry_can_be_disabled(self):
        session, _, _ = _session(
            FakeDsp408(faults=Faults(drop_replies=99)),
            pacing=Pacing(reply_timeout_s=0.02, idle_after_reply_s=0.0),
            faults=FaultPolicy(retry_on_timeout=False),
        )
        with pytest.raises(ReplyTimeout):
            session.poll_status()
        assert session.stats.retries == 0
        assert session.stats.timeouts == 1


class TestPacing:
    def test_it_waits_after_a_reply_before_the_next_request(self):
        session, _, clock = _session(
            pacing=Pacing(idle_after_reply_s=0.02, max_requests_per_s=1000.0)
        )
        session.poll_status()
        clock.slept.clear()
        session.poll_status()
        assert pytest.approx(0.02, abs=1e-9) == sum(clock.slept)

    def test_it_caps_the_sustained_request_rate(self):
        session, _, clock = _session(
            pacing=Pacing(idle_after_reply_s=0.0, max_requests_per_s=10.0)
        )
        session.poll_status()
        clock.slept.clear()
        session.poll_status()
        assert sum(clock.slept) == pytest.approx(0.1, abs=1e-9)

    def test_defaults_are_conservative_against_the_measurements(self):
        # Keeps the constants tied to evidence rather than to feel. Observed
        # maxima: 354 ms for a write, 339 ms for a read; turnaround floor
        # 2.1 ms; sustained 10.02 req/s.
        p = Pacing()
        assert p.reply_timeout_s >= 3 * 0.354
        assert p.idle_after_reply_s >= 0.0021
        assert p.max_requests_per_s <= 10.02
        assert p.retries == 1
        assert p.retry_delay_s == pytest.approx(3.2)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"reply_timeout_s": 0},
            {"reply_timeout_s": -1},
            {"max_requests_per_s": 0},
            {"retries": -1},
        ],
    )
    def test_nonsense_pacing_is_rejected(self, kwargs):
        with pytest.raises(ValueError):
            Pacing(**kwargs)


class TestLinkId:
    def test_every_frame_carries_the_observed_link_id(self):
        session, device, _ = _session()
        session.handshake()
        assert {int(f.bluetooth_device_id) for f in device.received} == {
            OBSERVED_BLUETOOTH_DEVICE_ID
        }

    def test_it_is_stamped_in_exactly_one_place(self):
        # A caller passing the default 0 gets it filled in, so no call site
        # has to remember.
        session, device, _ = _session()
        session.transact(
            Frame(frame_type=FrameType.READ, data_type=DataType.SYSTEM, channel_id=3)
        )
        assert int(device.received[-1].bluetooth_device_id) == 4

    def test_a_conflicting_link_id_is_refused_rather_than_overwritten(self):
        session, _, _ = _session()
        with pytest.raises(ProtocolViolation, match="inside the checksum"):
            session.transact(
                Frame(
                    frame_type=FrameType.READ,
                    data_type=DataType.SYSTEM,
                    channel_id=3,
                    bluetooth_device_id=7,
                )
            )

    def test_the_session_id_can_be_overridden_for_a_re_paired_phone(self):
        session, device, _ = _session(bluetooth_device_id=2)
        session.poll_status()
        assert int(device.received[-1].bluetooth_device_id) == 2


class TestFramingIntegration:
    def test_replies_arriving_as_20_byte_chunks_are_reassembled(self):
        # The bulk record is 312 bytes over 16 chunks -- the case most likely
        # to break partial buffering.
        session, _, _ = _session()
        record = session.read_channel_record(3)
        assert len(record) == 296

    def test_unpadded_replies_also_work(self):
        # Proves the reader does not depend on the padding it tolerates.
        session, _, _ = _session(FakeDsp408(faults=Faults(no_chunking=True)))
        assert len(session.read_channel_record(0)) == 296

    def test_garbage_between_frames_is_resynced_past(self):
        device = FakeDsp408(faults=Faults(garbage_prefix=b"\x11\x22\x33"))
        session, _, _ = _session(device)
        assert session.read_system(4) == b"MYDW-AV1.06"
        assert session._reader.stats.resyncs >= 1

    def test_a_corrupted_reply_times_out_rather_than_being_accepted(self):
        session, _, _ = _session(
            FakeDsp408(faults=Faults(corrupt_checksum=99)),
            pacing=Pacing(reply_timeout_s=0.02, idle_after_reply_s=0.0),
        )
        with pytest.raises(ReplyTimeout):
            session.poll_status()


class TestPolicyIntegration:
    def test_writes_are_refused_by_default(self):
        session, device, _ = _session()
        with pytest.raises(TxRefused, match="not enabled"):
            session.write_block(0, 31, bytes(8))
        assert device.writes == []

    def test_a_refused_write_never_reaches_the_transport(self):
        session, device, _ = _session(policy=_armed_policy())
        with pytest.raises(TxRefused):
            session.write_block(0, 34, bytes(8))  # contradicted block
        assert device.writes == []
        assert session.stats.requests == 0

    def test_the_blast_radius_cap_is_enforced_through_the_session(self):
        policy = TxPolicy(allow_writes=True, blast_radius=BlastRadius(max_writes=2))
        session, _, _ = _session(policy=policy)
        session.write_block(0, 31, bytes(8))
        session.write_block(0, 32, bytes(8))
        with pytest.raises(TxRefused, match="blast radius"):
            session.write_block(0, 33, bytes(8))


class TestFakeRefusesToImprovise:
    """A fake that invents replies trains us against a device that isn't real."""

    def test_an_unobserved_system_channel_raises(self):
        session, _, _ = _session(policy=TxPolicy())
        # Bypass the policy to reach the fake directly.
        with pytest.raises(UnexpectedRequest, match="system read at channel_id"):
            session.transport.write(
                Frame(
                    frame_type=FrameType.READ,
                    data_type=DataType.SYSTEM,
                    channel_id=42,
                ).encode()
            )

    def test_a_bulk_write_raises(self):
        session, _, _ = _session()
        with pytest.raises(UnexpectedRequest, match="bulk write"):
            session.transport.write(
                Frame(
                    frame_type=FrameType.WRITE,
                    data_type=DataType.OUTPUT_CHANNEL,
                    channel_id=0,
                    data_id=119,
                    payload=bytes(8),
                ).encode()
            )

    def test_a_destructive_opcode_raises(self):
        session, _, _ = _session()
        raw = Frame(
            frame_type=FrameType.READ, data_type=DataType.SYSTEM, channel_id=96
        ).encode(allow_destructive=True)
        with pytest.raises(UnexpectedRequest, match="RESET_MCU"):
            session.transport.write(raw)


class TestPersistenceModel:
    def test_a_power_cycle_preserves_writes(self):
        # The intuition that a reboot reverts uncommitted changes is exactly
        # backwards on this device.
        session, device, _ = _session(policy=_armed_policy())
        session.write_block(0, 31, bytes([9] * 8))
        device.power_cycle()
        assert device.image.channels[0][248:256] == bytes([9] * 8)

    def test_a_preset_recall_destroys_them(self):
        session, device, _ = _session(policy=_armed_policy())
        device.store_preset(1)
        session.write_block(0, 31, bytes([9] * 8))
        device.recall_preset(1)
        assert device.image.channels[0][248:256] != bytes([9] * 8)

    def test_recalling_does_not_modify_the_preset(self):
        # Which is what makes a preset slot the one restore point that
        # survives everything.
        session, device, _ = _session(policy=_armed_policy())
        device.store_preset(2)
        before = device.image.presets[1]
        session.write_block(0, 31, bytes([7] * 8))
        device.recall_preset(2)
        session.write_block(0, 31, bytes([8] * 8))
        assert device.image.presets[1] == before


class TestTransportLifecycle:
    def test_using_a_closed_transport_raises(self):
        device = FakeDsp408()
        transport = LoopbackTransport(device)
        with pytest.raises(NotConnected):
            transport.write(b"x")

    def test_the_session_is_a_context_manager(self):
        device = FakeDsp408()
        with Dsp408Session(LoopbackTransport(device)) as session:
            session.poll_status()
            assert session.transport.is_open
        assert not device.connected

    def test_close_sends_no_goodbye(self):
        # There is no application-layer shutdown frame; the capture's session
        # simply stopped.
        session, device, _ = _session()
        session.poll_status()
        seen = len(device.received)
        session.close()
        assert len(device.received) == seen


class TestPresets:
    """Store and recall -- the rollback that needs none of our code to be right.

    Measured 2026-08-09 from ``captures/btsnoop_hci_2026-08-09_presets.log``.
    The shape is unusual enough to be worth pinning frame by frame: a recall is
    **eight reads**, a store is a name write followed by eight 296-byte writes,
    and the slot travels in ``user_id`` rather than anywhere in the payload.
    """

    @staticmethod
    def _preset_policy() -> TxPolicy:
        return TxPolicy(
            allow_presets=True,
            allow_writes=True,
            blast_radius=BlastRadius(max_writes=999, max_channels=8),
        )

    def _open(self):
        return _session(policy=self._preset_policy())

    def test_a_recall_is_eight_reads_and_no_writes(self):
        session, device, _ = self._open()
        session.recall_preset(3)

        reads = [f for f in device.received if int(f.frame_type) == FrameType.READ]
        assert len(reads) == 8
        assert [f.channel_id for f in reads] == list(range(8))
        assert {f.data_id for f in reads} == {0}
        assert {f.user_id for f in reads} == {3}
        assert device.writes == []

    def test_the_recall_really_replaces_the_working_area(self):
        # The whole reason this had to be modelled rather than treated as a
        # read: eight reads change the tune.
        session, device, _ = self._open()
        device.store_preset(2)
        device.image.channels[0][248] = 0  # mute output 1 in the working area
        assert device.image.channels[0][248] == 0

        session.recall_preset(2)
        assert device.image.channels[0][248] == 1
        # One per read. Whether the real device loads on the first
        # slot-addressed read or on every one is unobserved -- only the full
        # eight-read sequence was ever captured -- but the operation is
        # idempotent, so the fake performing it eight times is indistinguishable
        # from performing it once and is the more conservative model.
        assert device.recalls == [2] * 8

    def test_a_recall_returns_what_it_loaded(self):
        session, device, _ = self._open()
        records = session.recall_preset(1)
        assert len(records) == 8
        assert all(len(r) == 296 for r in records)
        assert list(records) == device.image.snapshot()

    def test_a_store_is_a_name_then_eight_records(self):
        session, device, _ = self._open()
        session.store_preset(5, "baseline", device.image.snapshot())

        assert len(device.writes) == 9
        name = device.writes[0]
        assert int(name.data_type) == DataType.SYSTEM
        assert (name.channel_id, name.data_id, name.user_id) == (0, 0, 5)
        assert len(name.payload) == 16

        for output, frame in enumerate(device.writes[1:]):
            assert int(frame.data_type) == DataType.OUTPUT_CHANNEL
            assert (frame.channel_id, frame.data_id, frame.user_id) == (output, 0, 5)
            assert len(frame.payload) == 296

    def test_a_store_does_not_touch_the_working_area(self):
        # The safe half of the pair, and the reason a store needs a lighter
        # authorisation than a recall.
        session, device, _ = self._open()
        before = device.image.snapshot()
        blank = [bytes(296)] * 8
        session.store_preset(6, "scratch", blank)
        assert device.image.snapshot() == before

    def test_store_then_recall_round_trips_the_whole_device(self):
        # The end-to-end claim the improvement invariant now rests on.
        session, device, _ = self._open()
        baseline = device.image.snapshot()
        session.store_preset(4, "baseline", baseline)

        # Wreck the working area the way a failed tuning run would.
        for ch in range(8):
            device.image.channels[ch][248:256] = bytes(8)
        assert device.image.snapshot() != baseline

        session.recall_preset(4)
        assert device.image.snapshot() == baseline

    def test_the_name_is_padded_and_carries_the_observed_trailer(self):
        session, device, _ = self._open()
        session.store_preset(1, "abc", device.image.snapshot())
        payload = device.writes[0].payload
        assert payload[:3] == b"abc"
        assert payload[3:15] == bytes(12)
        assert payload[15] == 0x26
        assert device.image.preset_names[0] == "abc"

    def test_an_over_long_name_is_truncated_to_the_field(self):
        session, device, _ = self._open()
        session.store_preset(1, "x" * 40, device.image.snapshot())
        assert len(device.writes[0].payload) == 16

    @pytest.mark.parametrize("slot", [0, 7, 9, 15, 16])
    def test_slots_outside_one_to_six_are_refused(self, slot):
        session, _, _ = self._open()
        with pytest.raises(ValueError, match="outside 1-6"):
            session.recall_preset(slot)
        with pytest.raises(ValueError, match="outside 1-6"):
            session.store_preset(slot, "x", [bytes(296)] * 8)

    def test_presets_are_refused_unless_enabled(self):
        # The default policy must not permit a frame that replaces the tune.
        session, _, _ = _session(policy=TxPolicy())
        with pytest.raises(TxRefused, match="preset recall"):
            session.recall_preset(2)

    def test_a_store_needs_the_preset_key_not_just_the_write_key(self):
        session, _, _ = _session(policy=_armed_policy())
        with pytest.raises(TxRefused, match="preset operations are not enabled"):
            session.store_preset(2, "x", [bytes(296)] * 8)

    def test_a_short_record_is_rejected_before_anything_is_sent(self):
        session, device, _ = self._open()
        with pytest.raises(ValueError, match="need 296"):
            session.store_preset(1, "x", [bytes(296)] * 7 + [bytes(8)])
        assert device.writes == []

    def test_preset_writes_do_not_consume_the_channel_budget(self):
        # A store is inherently all eight channels. Counting it against
        # max_channels would forbid the operation rather than bound it -- and
        # would exhaust the budget for the tune the store exists to protect.
        policy = TxPolicy(
            allow_presets=True,
            allow_writes=True,
            blast_radius=BlastRadius(max_writes=999, max_channels=1),
        )
        session, device, _ = _session(policy=policy)
        session.store_preset(3, "baseline", device.image.snapshot())
        assert policy.channels_written == set()
        assert policy.preset_writes == 9
        # And an ordinary single-channel write is still available afterwards.
        session.write_block(0, 31, bytes(8))
        assert policy.channels_written == {0}


class TestLinkIdMismatch:
    """The most likely way first contact fails, made self-diagnosing.

    ``bluetooth_device_id`` is 4 in all 5834 frames of the phone capture and is
    almost certainly a host-side index into *that phone's* paired-device list.
    A PC pairing plausibly is not 4. The field is inside the echoed header, so
    a disagreement makes every reply fail the echo match.

    Without this, the symptom is eight discarded replies and "the link is out
    of step", or a bare timeout -- neither of which points anywhere near the
    cause. An adversarial review flagged it as the top first-contact risk and
    noted the round-2 mitigation had never landed. It had not.
    """

    @staticmethod
    def _echo_a_different_link_id(device, value):
        original = device._emit

        def patched(request, ack, payload):
            return original(replace(request, bluetooth_device_id=value), ack, payload)

        device._emit = patched

    def test_it_raises_immediately_and_names_the_value(self):
        device = FakeDsp408()
        self._echo_a_different_link_id(device, 2)
        session, _, _ = _session(device)
        with pytest.raises(LinkIdMismatch) as excinfo:
            session.poll_status()
        text = str(excinfo.value)
        assert "echoed 2" in text
        assert "--link-id 2" in text

    def test_it_does_not_wait_for_the_stale_reply_budget(self):
        # Eight discards then a generic error would be technically correct and
        # practically useless. One reply is enough to know.
        device = FakeDsp408()
        self._echo_a_different_link_id(device, 7)
        session, _, _ = _session(device)
        with pytest.raises(LinkIdMismatch):
            session.poll_status()
        assert session.stats.stale_replies == 0

    def test_a_genuinely_stale_reply_is_still_just_discarded(self):
        # The new check must not swallow the case it sits next to: a reply that
        # differs in a *real* field is a late answer to an old request, and
        # discarding it is correct.
        device = FakeDsp408()
        original = device._emit

        def patched(request, ack, payload):
            return original(replace(request, data_id=99), ack, payload)

        device._emit = patched
        session, _, _ = _session(device)
        with pytest.raises((ReplyTimeout, ProtocolViolation)):
            session.poll_status()
        assert session.stats.stale_replies >= 1

    def test_the_session_takes_the_id_as_a_constructor_argument(self):
        device = FakeDsp408()
        self._echo_a_different_link_id(device, 2)
        session, _, _ = _session(device, bluetooth_device_id=2)
        session.poll_status()
        assert session.stats.clean


class TestSuspectedUsbArbitration:
    """The measured failure mode of a second control transport holding the device.

    2026-08-11: with the vendor app on USB-B, the DSP-408 accepts the RFCOMM
    link and then answers nothing at all. The project spent a bench session
    concluding the encoding was at fault before the operator volunteered the
    USB session, so the signature is worth recognising in code.
    """

    def test_silence_from_the_first_transaction_is_named(self, tmp_path):
        session = _mute_session()
        with pytest.raises(SuspectedUsbArbitration) as exc:
            session.read_system(4)
        assert "USB-B" in str(exc.value)
        assert "Nothing has ever replied" in str(exc.value)

    def test_it_is_still_a_reply_timeout(self, tmp_path):
        # Subclass, so every existing handler keeps working.
        session = _mute_session()
        with pytest.raises(ReplyTimeout):
            session.read_system(4)

    def test_a_timeout_after_a_good_reply_is_an_ordinary_timeout(self, tmp_path):
        # Mid-session silence means something else, and must not be blamed on
        # USB -- a wrong diagnosis is worse than a generic one.
        transport = _RelentingTransport()
        session = Dsp408Session(
            transport,
            policy=TxPolicy(),
            pacing=Pacing(
                idle_after_reply_s=0.0,
                max_requests_per_s=1e9,
                reply_timeout_s=0.05,
                retries=0,
            ),
        )
        session.open()
        session.read_system(4)  # a real reply first
        transport.go_silent()  # then the link dies mid-session
        with pytest.raises(ReplyTimeout) as exc:
            session.read_system(4)
        assert not isinstance(exc.value, SuspectedUsbArbitration)
        assert "USB-B" not in str(exc.value)


class _MuteTransport(Transport):
    """A link that opens, accepts every byte, and never answers.

    Exactly what the DSP-408 does while the vendor app holds it over USB-B:
    the socket is fine, the bytes go out, and nothing comes back.
    """

    def __init__(self) -> None:
        self._open = False
        self.sent = bytearray()

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes) -> None:
        self.sent += data

    def read(self, timeout_s: float) -> bytes:
        del timeout_s
        return b""

    @property
    def is_open(self) -> bool:
        return self._open


def _mute_session() -> Dsp408Session:
    """A session whose device never answers anything."""
    session = Dsp408Session(
        _MuteTransport(),
        policy=TxPolicy(),
        pacing=Pacing(
            idle_after_reply_s=0.0,
            max_requests_per_s=1e9,
            reply_timeout_s=0.05,
            retries=0,
        ),
    )
    session.open()
    return session


class _RelentingTransport(LoopbackTransport):
    """A normal loopback that can be made to stop answering."""

    def __init__(self) -> None:
        super().__init__(FakeDsp408())
        self._silent = False

    def go_silent(self) -> None:
        self._silent = True

    def read(self, timeout_s: float) -> bytes:
        if self._silent:
            return b""
        return super().read(timeout_s)


class _DroppingTransport(LoopbackTransport):
    """A loopback whose socket closes itself after a set number of reads.

    An idle RFCOMM link that times out at the far end looks like this: no
    error, no goodbye, the socket simply stops being open.
    """

    def __init__(self, drop_after: int) -> None:
        super().__init__(FakeDsp408())
        self._reads = 0
        self._drop_after = drop_after

    def read(self, timeout_s: float) -> bytes:
        self._reads += 1
        if self._reads > self._drop_after:
            self.close()
            return b""
        return super().read(timeout_s)


class _ChattyTransport(LoopbackTransport):
    """A loopback that also volunteers some bytes nobody asked for."""

    def __init__(self, unsolicited: bytes) -> None:
        super().__init__(FakeDsp408())
        self._pending = bytes(unsolicited)

    def read(self, timeout_s: float) -> bytes:
        if self._pending:
            out, self._pending = self._pending, b""
            return out
        return super().read(timeout_s)


class TestIdleSurvival:
    """Bring-up Stage 2: does the link need the app's 10 Hz poll to stay up?

    The vendor app polls ``dt9/ch3`` continuously for all 286 s of the
    capture, so the capture cannot distinguish a required keepalive from a UI
    refresh. Our pacing assumes it is not required. This measures it.
    """

    def test_it_sends_nothing_during_the_window(self):
        # The whole point. A probe inside the window would supply exactly the
        # traffic being tested for, and the result would mean nothing.
        session, _, _ = _session()
        session.handshake()
        before = session.stats.requests
        session.measure_idle_survival(1.0)
        # Exactly one: the confirming read *after* the window.
        assert session.stats.requests == before + 1

    def test_a_surviving_link_reports_a_lower_bound_not_a_timeout(self):
        session, _, _ = _session()
        result = session.measure_idle_survival(2.0)
        assert result.survived
        assert result.dropped_at_s is None
        assert result.probe_ok
        assert "LOWER BOUND" in result.summary()

    def test_a_self_closing_socket_is_recorded_with_its_timing(self):
        clock = FakeClock()
        session = Dsp408Session(
            _DroppingTransport(drop_after=3), clock=clock, sleep=clock.sleep
        )
        session.open()
        result = session.measure_idle_survival(5.0)
        assert not result.survived
        assert result.dropped_at_s is not None
        assert result.dropped_at_s < 5.0
        assert "dropped on its own" in result.summary()

    def test_an_open_socket_that_no_longer_answers_is_not_survival(self):
        # Both halves of `survived` are load-bearing. This is a dead link that
        # has not noticed -- the failure mode the USB-arbitration session
        # already showed this device is capable of producing.
        transport = _RelentingTransport()
        clock = FakeClock()
        session = Dsp408Session(
            transport,
            pacing=Pacing(reply_timeout_s=0.05, retries=0, idle_after_reply_s=0.0),
            clock=clock,
            sleep=clock.sleep,
        )
        session.open()
        transport.go_silent()
        result = session.measure_idle_survival(1.0)
        assert result.dropped_at_s is None  # the socket never closed
        assert not result.probe_ok  # and it answered nothing
        assert not result.survived
        assert "stayed open" in result.summary()

    def test_unsolicited_frames_are_framed_and_reported(self):
        # Never observed in 5834 frames. If it happens it is a new fact about
        # the device, and swallowing it here is how it would stay unknown.
        stray = Frame(
            frame_type=Ack.DATA,
            data_type=DataType.SYSTEM,
            channel_id=3,
            data_id=0,
            payload=b"\x01\x02\x03\x04\x05\x06\x07\x08",
        )
        transport = _ChattyTransport(stray.encode())
        clock = FakeClock()
        session = Dsp408Session(transport, clock=clock, sleep=clock.sleep)
        session.open()
        result = session.measure_idle_survival(1.0)
        assert len(result.unsolicited) == 1
        assert result.unsolicited[0].channel_id == 3

    def test_a_zero_window_is_a_programming_error(self):
        session, _, _ = _session()
        with pytest.raises(ValueError):
            session.measure_idle_survival(0)

    def test_a_poisoned_session_refuses(self):
        session, _, _ = _session()
        session._poison("test")
        with pytest.raises(SessionPoisoned):
            session.measure_idle_survival(1.0)

    def test_survived_needs_both_halves(self):
        # Guarding the property directly, because a future edit that drops one
        # conjunct would still pass every test above except this one.
        base = dict(requested_s=1.0, waited_s=1.0, unsolicited=(), probe_error=None)
        assert IdleSurvival(dropped_at_s=None, probe_ok=True, **base).survived
        assert not IdleSurvival(dropped_at_s=None, probe_ok=False, **base).survived
        assert not IdleSurvival(dropped_at_s=0.5, probe_ok=True, **base).survived
