"""The engineering-units backend.

Two concerns, tested separately:

* **Translation.** Engineering units in, correct device bytes out, and back
  again -- checked against the fake, which holds real records.
* **Refusal.** Everything this backend will not do, because ``ChannelConfig``
  can express more than the device's decoded fields can honour. A backend that
  quietly drops a field produces a device that does not match the model the
  optimizer is reasoning about, and the improvement invariant then compares a
  prediction against a system that was never configured as predicted.

The last class holds both backends to one contract, so the simulator and the
hardware route cannot drift into meaning different things.
"""

from __future__ import annotations

import hashlib

import pytest

from tuner.dsp.backend import (
    Biquad,
    BudgetExceeded,
    ChannelConfig,
    Crossover,
    FilterType,
)
from tuner.dsp.device import (
    BLOCK_LEN,
    Dsp408Device,
    SnapshotEvidence,
    UnsendablePlan,
    UnverifiedBlock,
    WriteJournal,
)
from tuner.dsp.dsp408_spp import (
    ADDRESSABLE_BANDS,
    FLAT_LEVEL_RAW,
    Dsp408Spp,
    PeqPolicy,
    UnsupportedRequest,
    _nudged_variants,
)
from tuner.dsp.fake_device import FakeDsp408
from tuner.dsp.protocol import (
    DataType,
    EqBand,
    Frame,
    FrameType,
    OutputBlock,
    OutputMisc,
    OutputMix,
    OutputXover,
    UnsendableFrame,
)
from tuner.dsp.session import (
    OBSERVED_BLUETOOTH_DEVICE_ID,
    Dsp408Session,
    Pacing,
)
from tuner.dsp.sim import SimulatedDsp
from tuner.dsp.transport import LoopbackTransport
from tuner.dsp.txpolicy import BlastRadius, TxPolicy


def _backend(tmp_path, policy=PeqPolicy.LEADING, nudge=True):
    fake = FakeDsp408()
    session = Dsp408Session(
        LoopbackTransport(fake),
        policy=TxPolicy(
            allow_writes=True,
            blast_radius=BlastRadius(max_writes=999, max_channels=8),
        ),
        pacing=Pacing(idle_after_reply_s=0.0, max_requests_per_s=1e9),
    )
    device = Dsp408Device(
        session, journal=WriteJournal(tmp_path / "j.jsonl"), session_id="test"
    )
    backend = Dsp408Spp(device=device, peq_policy=policy, nudge_unsendable=nudge)
    backend.connect()

    path = tmp_path / "snap.bin"
    path.write_bytes(b"".join(device.refresh_all()))
    device.arm_writes(
        "tests",
        SnapshotEvidence(
            path=path,
            digest=hashlib.sha256(path.read_bytes()).hexdigest(),
            firmware="MYDW-AV1.06",
            session_id="test",
        ),
    )
    return backend, fake


@pytest.fixture
def backend(tmp_path):
    return _backend(tmp_path)


def _block(record, index):
    return record[index * BLOCK_LEN : (index + 1) * BLOCK_LEN]


def _flat(**kw):
    """A config that matches the fake's stored state except where overridden."""
    base = {
        "gain_dbfs": -10.0,
        "delay_samples": 0,
        "crossover": Crossover(high_pass_hz=450.0, low_pass_hz=3500.0, slope_db_oct=24),
        "peq": (),
        "muted": False,
    }
    base.update(kw)
    return ChannelConfig(**base)


class TestTranslation:
    def test_gain_round_trips(self, backend):
        be, fake = backend
        be.write_channel(0, _flat(gain_dbfs=-6.0))
        misc = OutputMisc.decode(_block(fake.image.channels[0], int(OutputBlock.MISC)))
        assert misc.gain_raw == 540  # -6 dB = 600 - 60
        assert be.read_channel(0).gain_dbfs == pytest.approx(-6.0)

    def test_delay_round_trips_in_samples(self, backend):
        be, fake = backend
        be.write_channel(0, _flat(delay_samples=137))
        misc = OutputMisc.decode(_block(fake.image.channels[0], int(OutputBlock.MISC)))
        assert misc.delay_raw == 137
        assert be.read_channel(0).delay_samples == 137

    def test_crossover_frequencies_are_hz_not_table_indices(self, backend):
        # 1234 is in no table, and the app was observed sending it verbatim.
        be, fake = backend
        be.write_channel(
            0,
            _flat(
                crossover=Crossover(
                    high_pass_hz=80.0, low_pass_hz=1234.0, slope_db_oct=24
                )
            ),
        )
        xover = OutputXover.decode(
            _block(fake.image.channels[0], int(OutputBlock.XOVER))
        )
        assert (xover.h_freq, xover.l_freq) == (80, 1234)

    def test_a_peq_band_round_trips(self, backend):
        be, _ = backend
        be.write_channel(0, _flat(peq=(Biquad(freq_hz=2500.0, gain_dbfs=-4.5, q=3.0),)))
        (band,) = be.read_channel(0).peq
        assert band.freq_hz == pytest.approx(2500.0)
        assert band.gain_dbfs == pytest.approx(-4.5)
        assert band.kind is FilterType.PEAKING

    def test_q_never_exceeds_what_was_requested(self, backend):
        # Bandwidth is the quantized parameter and rounds UP, so the achieved
        # filter is never narrower than asked for. A narrower filter than
        # requested is the more surprising failure, so the device errs wide.
        be, _ = backend
        for requested in (0.7, 1.0, 2.0, 4.0, 8.0):
            be.write_channel(
                0, _flat(peq=(Biquad(freq_hz=1000.0, gain_dbfs=-3.0, q=requested),))
            )
            (band,) = be.read_channel(0).peq
            assert band.q <= requested + 1e-9, requested

    def test_several_bands_land_in_order(self, backend):
        be, fake = backend
        bands = tuple(
            Biquad(freq_hz=500.0 * (i + 1), gain_dbfs=-2.0, q=2.0) for i in range(4)
        )
        be.write_channel(0, _flat(peq=bands))
        for i, expected in enumerate(bands):
            stored = EqBand.decode(_block(fake.image.channels[0], i))
            assert stored.freq == int(expected.freq_hz)


class TestCarriedThrough:
    """Fields ``ChannelConfig`` cannot express must survive a write."""

    def test_writing_gain_preserves_polarity_and_speaker_type(self, backend):
        # The exact bug read-modify-write exists to prevent.
        be, fake = backend
        before = OutputMisc.decode(
            _block(fake.image.channels[0], int(OutputBlock.MISC))
        )
        be.write_channel(0, _flat(gain_dbfs=-20.0))
        after = OutputMisc.decode(_block(fake.image.channels[0], int(OutputBlock.MISC)))
        assert after.gain_raw != before.gain_raw
        assert (after.polar, after.spk_type, after.eq_mode, after.enabled) == (
            before.polar,
            before.spk_type,
            before.eq_mode,
            before.enabled,
        )

    def test_writing_a_crossover_preserves_the_alignment_bytes(self, backend):
        # Alignment (Linkwitz-Riley / Butterworth / Bessel / Defeat) is mapped
        # but ChannelConfig cannot express it, so it must survive a write
        # untouched rather than being defaulted to zero.
        be, fake = backend
        before = OutputXover.decode(
            _block(fake.image.channels[0], int(OutputBlock.XOVER))
        )
        be.write_channel(
            0,
            _flat(
                crossover=Crossover(
                    high_pass_hz=120.0, low_pass_hz=5000.0, slope_db_oct=24
                )
            ),
        )
        after = OutputXover.decode(
            _block(fake.image.channels[0], int(OutputBlock.XOVER))
        )
        assert (after.h_filter, after.l_filter) == (before.h_filter, before.l_filter)

    def test_the_undecoded_blocks_are_never_touched(self, backend):
        be, fake = backend
        before = bytes(fake.image.channels[0])
        be.write_channel(0, _flat(gain_dbfs=-20.0, peq=(Biquad(1000.0, -3.0, 2.0),)))
        after = bytes(fake.image.channels[0])
        for index in (33, 34, 35, 36):  # mix, the contradicted pair, name
            assert _block(after, index) == _block(before, index), index

    def test_a_none_crossover_leaves_the_corner_alone(self, backend):
        # None means "do not change", not "disable". A high-pass silently
        # vanishing is the failure mode that kills tweeters.
        be, fake = backend
        before = OutputXover.decode(
            _block(fake.image.channels[0], int(OutputBlock.XOVER))
        )
        be.write_channel(
            0,
            _flat(
                crossover=Crossover(
                    high_pass_hz=None, low_pass_hz=None, slope_db_oct=24
                )
            ),
        )
        after = OutputXover.decode(
            _block(fake.image.channels[0], int(OutputBlock.XOVER))
        )
        assert (after.h_freq, after.l_freq) == (before.h_freq, before.l_freq)


class TestCrossoverSlope:
    """Writable since 2026-08-09, when the selector bytes were mapped.

    Fourteen single-control A/Bs in the vendor app -- slopes 6/12/18/24 on both
    filters, then the three alignments -- moved exactly one byte each and gave
    ``level = slope/6 - 1`` with alignment orthogonal to it. The mapping is
    corroborated acoustically: the OUT5 configuration that fitted an LR4
    crossover to 0.247 dB rms carries ``l_filter = 0, l_level = 3``, which this
    table reads as Linkwitz-Riley 24 dB/octave.
    """

    @pytest.mark.parametrize(("slope", "level"), [(6, 0), (12, 1), (18, 2), (24, 3)])
    def test_each_storable_slope_lands_on_its_byte(self, backend, slope, level):
        be, fake = backend
        be.write_channel(
            0,
            _flat(
                crossover=Crossover(
                    high_pass_hz=450.0, low_pass_hz=3500.0, slope_db_oct=slope
                )
            ),
        )
        after = OutputXover.decode(
            _block(fake.image.channels[0], int(OutputBlock.XOVER))
        )
        assert (after.h_level, after.l_level) == (level, level)

    def test_it_reads_back_as_the_slope_that_was_written(self, backend):
        be, _ = backend
        for slope in (6, 12, 18, 24):
            be.write_channel(
                0,
                _flat(
                    crossover=Crossover(
                        high_pass_hz=450.0, low_pass_hz=3500.0, slope_db_oct=slope
                    )
                ),
            )
            assert be.read_channel(0).crossover.slope_db_oct == slope

    @pytest.mark.parametrize("slope", [0, 3, 15, 36, 48])
    def test_an_unstorable_slope_raises_rather_than_rounding(self, backend, slope):
        # Rounding 15 to 12 or 18 would leave the device disagreeing with the
        # model the optimizer reasoned about -- the failure the improvement
        # invariant is least able to see, because both sides look plausible.
        be, fake = backend
        with pytest.raises(UnsupportedRequest, match="not storable"):
            be.write_channel(
                0,
                _flat(
                    crossover=Crossover(
                        high_pass_hz=450.0, low_pass_hz=3500.0, slope_db_oct=slope
                    )
                ),
            )
        assert fake.writes == []


class TestRefusals:
    def test_it_refuses_more_bands_than_the_budget_allows(self, backend):
        be, fake = backend
        bands = tuple(
            Biquad(100.0 * (i + 1), -1.0, 2.0)
            for i in range(be.limits.max_peq_per_channel + 1)
        )
        with pytest.raises(UnsupportedRequest, match="addressable is not"):
            be.write_channel(0, _flat(peq=bands))
        assert fake.writes == []

    def test_it_refuses_a_non_peaking_band(self, backend):
        be, _ = backend
        shelf = Biquad(1000.0, -3.0, 1.0, kind=FilterType.LOW_SHELF)
        with pytest.raises(UnsupportedRequest, match="only peaking"):
            be.write_channel(0, _flat(peq=(shelf,)))

    @pytest.mark.parametrize("freq", [0.0, 25_000.0])
    def test_it_refuses_an_out_of_range_band_frequency(self, backend, freq):
        be, _ = backend
        with pytest.raises(UnsupportedRequest, match="out of range"):
            be.write_channel(0, _flat(peq=(Biquad(freq, -3.0, 2.0),)))

    @pytest.mark.parametrize("output", [-1, 8, 99])
    def test_it_refuses_a_bad_output(self, backend, output):
        # IndexError, matching SimulatedDsp -- see TestBackendContract.
        be, _ = backend
        with pytest.raises(IndexError, match="out of range"):
            be.write_channel(output, _flat())

    def test_refusals_happen_before_anything_is_written(self, backend):
        # Ordering matters: a refusal after a partial write leaves the channel
        # in a state nobody asked for.
        be, fake = backend
        before = bytes(fake.image.channels[0])
        bad = _flat(
            gain_dbfs=-30.0,
            crossover=Crossover(
                high_pass_hz=450.0, low_pass_hz=3500.0, slope_db_oct=48
            ),
        )
        with pytest.raises(UnsupportedRequest):
            be.write_channel(0, bad)
        assert bytes(fake.image.channels[0]) == before


class TestPeqPolicy:
    def test_there_is_no_default(self):
        # Construction must fail until the caller chooses, because neither
        # reading is safe to assume.
        with pytest.raises(TypeError):
            Dsp408Spp(device=None)  # type: ignore[call-arg]

    def test_leading_leaves_other_bands_alone(self, tmp_path):
        be, fake = _backend(tmp_path, PeqPolicy.LEADING)
        fake.image.channels[0][5 * 8 : 6 * 8] = EqBand(
            freq=800, level=650, bw=40
        ).encode()
        be.write_channel(0, _flat(peq=(Biquad(1000.0, -3.0, 2.0),)))
        leftover = EqBand.decode(_block(fake.image.channels[0], 5))
        assert leftover.level == 650  # untouched, and still in circuit

    def test_exclusive_flattens_the_rest(self, tmp_path):
        be, fake = _backend(tmp_path, PeqPolicy.EXCLUSIVE)
        fake.image.channels[0][5 * 8 : 6 * 8] = EqBand(
            freq=800, level=650, bw=40
        ).encode()
        be.write_channel(0, _flat(peq=(Biquad(1000.0, -3.0, 2.0),)))
        flattened = EqBand.decode(_block(fake.image.channels[0], 5))
        assert flattened.level == FLAT_LEVEL_RAW
        assert flattened.freq == 800  # frequency preserved, gain removed

    def test_exclusive_does_not_rewrite_already_flat_bands(self, tmp_path):
        # Fewest writes is fewest risks.
        be, fake = _backend(tmp_path, PeqPolicy.EXCLUSIVE)
        for i in range(ADDRESSABLE_BANDS):
            fake.image.channels[0][i * 8 : (i + 1) * 8] = EqBand(
                freq=1000, level=FLAT_LEVEL_RAW, bw=40
            ).encode()
        fake.writes.clear()
        be.write_channel(0, _flat(peq=(Biquad(1000.0, -3.0, 2.0),)))
        assert len(fake.writes) <= 3  # the band, misc, xover


class TestBackendContract:
    """Both backends held to one contract, so they cannot drift apart."""

    @pytest.fixture(params=["sim", "spp"])
    def any_backend(self, request, tmp_path):
        if request.param == "sim":
            be = SimulatedDsp()
            be.connect()
            return be
        return _backend(tmp_path)[0]

    def test_limits_are_exposed(self, any_backend):
        limits = any_backend.limits
        assert limits.n_outputs == 8
        assert limits.sample_rate_hz == 48_000
        # Measured since 2026-08-12: channel counts from the wire, the chip
        # map on a logic analyser, and both ceilings from the vendor UI.
        assert limits.measured is True
        assert limits.max_delay_samples_per_output == 384
        assert limits.max_peq_per_channel == 10

    def test_read_channel_returns_a_config(self, any_backend):
        config = any_backend.read_channel(0)
        assert isinstance(config, ChannelConfig)
        assert isinstance(config.crossover, Crossover)

    def test_a_written_gain_reads_back(self, any_backend):
        config = any_backend.read_channel(0)
        from dataclasses import replace

        any_backend.write_channel(0, replace(config, gain_dbfs=-8.0))
        assert any_backend.read_channel(0).gain_dbfs == pytest.approx(-8.0)

    def test_an_out_of_range_output_raises(self, any_backend):
        # Both backends must signal this the same way. On this test's first
        # run they did not: Dsp408Spp leaked DeviceError from the record
        # layer, so code written against the simulator would not have caught
        # it. That is the drift a shared contract exists to find.
        with pytest.raises(IndexError):
            any_backend.read_channel(99)
        with pytest.raises(IndexError):
            any_backend.write_channel(99, any_backend.read_channel(0))

    def test_too_many_bands_is_refused(self, any_backend):
        from dataclasses import replace

        config = any_backend.read_channel(0)
        bands = tuple(
            Biquad(100.0 * (i + 1), -1.0, 2.0)
            for i in range(any_backend.limits.max_peq_per_channel + 1)
        )
        # The two signal it differently -- BudgetExceeded from the simulator's
        # resource model, UnsupportedRequest from the backend's. Both are
        # ValueError-rooted refusals; what the contract fixes is that neither
        # silently truncates.
        with pytest.raises((BudgetExceeded, UnsupportedRequest)):
            any_backend.write_channel(0, replace(config, peq=bands))


class TestShelfBandsAreRefused:
    """A band stored as a shelf must not receive peaking parameters.

    ``EqBand.type`` was mapped on 2026-08-09 (0 PEQ, 1 low shelf, 2 high
    shelf). Before that the backend carried the field through blind, which was
    the right call while its meaning was unknown -- and became a hazard the
    moment it was known.

    ``optimize.biquad`` fits peaking sections, and ``bw`` does not mean what
    ``q_from_bw_raw`` says it means for a shelf. Writing a fitted band into a
    shelf slot leaves the device running a filter nobody modelled. Nothing
    would look wrong: the write succeeds, the readback matches, the fit was
    plausible. The improvement invariant would then measure a prediction
    against a system configured differently and attribute the difference to
    acoustics.

    So this mapping is the one that **added** a refusal.
    """

    @staticmethod
    def _make_shelf(fake, band: int, kind: int) -> None:
        blk = bytearray(_block(fake.image.channels[0], band))
        blk[7] = kind
        fake.image.channels[0][band * 8 : band * 8 + 8] = bytes(blk)

    @pytest.mark.parametrize("kind", [1, 2])
    def test_writing_over_a_shelf_raises(self, backend, kind):
        be, fake = backend
        self._make_shelf(fake, 0, kind)
        with pytest.raises(UnsupportedRequest, match="SHELF"):
            be.write_channel(
                0, _flat(peq=(Biquad(1000.0, 3.0, 2.0, FilterType.PEAKING),))
            )

    def test_the_message_says_what_to_do_about_it(self, backend):
        be, fake = backend
        self._make_shelf(fake, 0, 1)
        with pytest.raises(UnsupportedRequest) as excinfo:
            be.write_channel(
                0, _flat(peq=(Biquad(1000.0, 3.0, 2.0, FilterType.PEAKING),))
            )
        text = str(excinfo.value)
        assert "band 1" in text
        assert "LOW_SHELF" in text
        assert "vendor app" in text

    def test_a_shelf_beyond_the_written_bands_is_untouched_under_leading(self, backend):
        # LEADING writes only the bands it was given, so a shelf further up the
        # chain is none of its business.
        be, fake = backend
        self._make_shelf(fake, 20, 2)
        be.write_channel(0, _flat(peq=(Biquad(1000.0, 3.0, 2.0, FilterType.PEAKING),)))
        assert _block(fake.image.channels[0], 20)[7] == 2

    def test_peq_bands_still_write_normally(self, backend):
        # The guard must not cost us the ordinary case.
        be, fake = backend
        be.write_channel(0, _flat(peq=(Biquad(1000.0, 3.0, 2.0, FilterType.PEAKING),)))
        assert EqBand.decode(_block(fake.image.channels[0], 0)).freq == 1000


class TestPlanIsAtomic:
    """A whole-channel write is all-or-nothing on the wire.

    **Measured 2026-08-11 on real device state.** `write_channel` used to
    encode and transmit block by block. On OUT1 of the operator's device it
    wrote misc and one EQ band, then raised `UnsendableFrame` on the third
    block -- flattening band 3 (`freq` 2514, `bw` 42) produces a frame whose
    checksum computes to zero at `bluetooth_device_id` 4, which the vendor app
    never sends and this project refuses to. One channel in eight.

    The device has no undo, so a half-written channel matches no model anyone
    reasoned about, and the improvement invariant would then compare a
    prediction against it. Hence plan -> preflight -> apply.

    Found only because the rehearsal ran against a fake seeded from the real
    device. Against `DeviceImage.flat()` the same run wrote a single block and
    reported success.
    """

    #: The exact band that triggered it, from
    #: ``snapshots/2026-08-11_stage5.json``, OUT1 block 3, flattened.
    UNSENDABLE_BAND = EqBand(freq=2514, level=600, bw=42, shf_db=0, type=0)
    UNSENDABLE_AT = (0, 3)  # (channel_id, data_id) at bluetooth_device_id 4

    def test_the_triggering_frame_really_is_unsendable(self):
        # Pins the premise. If the protocol's zero-checksum rule ever changes,
        # this fails first and the tests below stop being about anything.
        ch, data_id = self.UNSENDABLE_AT
        frame = Frame(
            frame_type=FrameType.WRITE,
            data_type=DataType.OUTPUT_CHANNEL,
            channel_id=ch,
            data_id=data_id,
            payload=self.UNSENDABLE_BAND.encode(),
            bluetooth_device_id=OBSERVED_BLUETOOTH_DEVICE_ID,
        )
        with pytest.raises(UnsendableFrame):
            frame.encode()

    def test_the_same_payload_is_sendable_at_link_id_zero(self):
        # Why the first diagnosis was wrong, pinned so nobody repeats it.
        # `bluetooth_device_id` is inside the checksum, so a sendability check
        # built with the default 0 asks a different question from the one the
        # session will ask with 4 -- and answers it confidently.
        ch, data_id = self.UNSENDABLE_AT
        Frame(
            frame_type=FrameType.WRITE,
            data_type=DataType.OUTPUT_CHANNEL,
            channel_id=ch,
            data_id=data_id,
            payload=self.UNSENDABLE_BAND.encode(),
            bluetooth_device_id=0,
        ).encode()  # does not raise

    def test_block_write_frame_stamps_the_sessions_link_id(self, tmp_path):
        be, _ = _backend(tmp_path)
        frame = be.device.session.block_write_frame(0, 31, bytes(8))
        assert int(frame.bluetooth_device_id) == OBSERVED_BLUETOOTH_DEVICE_ID

    def test_plan_channel_transmits_nothing(self, tmp_path):
        be, fake = _backend(tmp_path, PeqPolicy.EXCLUSIVE)
        plan = be.plan_channel(0, _flat(gain_dbfs=-6.0))
        assert plan
        assert fake.writes == []

    def test_an_unsendable_plan_writes_nothing(self, tmp_path):
        # With nudging off, which is how the refusal path is reached on
        # purpose. With it on the plan is repaired instead -- see
        # TestNudgeUnsendable below.
        be, fake = _backend(tmp_path, PeqPolicy.EXCLUSIVE, nudge=False)
        ch, data_id = self.UNSENDABLE_AT
        # Put the offending band on the device so EXCLUSIVE has to flatten it.
        raw = self.UNSENDABLE_BAND.encode()
        stored = EqBand(freq=2514, level=480, bw=42, shf_db=0, type=0)
        fake.image.channels[ch][data_id * BLOCK_LEN : (data_id + 1) * BLOCK_LEN] = (
            stored.encode()
        )
        be.device.refresh(ch)

        with pytest.raises(UnsendablePlan) as exc:
            be.write_channel(ch, _flat(gain_dbfs=-6.0))

        assert f"block {data_id}" in str(exc.value)
        assert raw.hex(" ") in str(exc.value)
        # The whole point: not one block went out, including the ones earlier
        # in the plan that would have encoded perfectly.
        assert fake.writes == []

    def test_a_sendable_plan_still_writes(self, tmp_path):
        # Guards against a preflight that refuses everything.
        be, fake = _backend(tmp_path, PeqPolicy.EXCLUSIVE)
        be.write_channel(0, _flat(gain_dbfs=-6.0))
        assert fake.writes

    def test_preflight_refuses_a_contradicted_block(self, tmp_path):
        be, fake = _backend(tmp_path)
        with pytest.raises(UnverifiedBlock):
            be.device.preflight(0, [(34, bytes(8))])
        assert fake.writes == []


class TestNudgeUnsendable:
    """Unsendable blocks are repaired by the smallest step, and reported.

    **Measured 2026-08-11**: the frame checksum is 8 bits, so about 1 block
    write in 272 cannot be transmitted at all. A tune writing 100 blocks
    across eight channels therefore has roughly a 31% chance of containing at
    least one. A backend that only refuses is correct and useless -- a third
    of tunes would fail to load for a reason unrelated to audio.

    So the plan is repaired. Never silently: the adjustment is recorded on the
    block in engineering units, and `preflight` still runs afterwards.
    """

    def test_the_plan_becomes_sendable(self, tmp_path):
        be, fake = _backend(tmp_path, PeqPolicy.EXCLUSIVE, nudge=True)
        ch, data_id = TestPlanIsAtomic.UNSENDABLE_AT
        stored = EqBand(freq=2514, level=480, bw=42, shf_db=0, type=0)
        fake.image.channels[ch][data_id * BLOCK_LEN : (data_id + 1) * BLOCK_LEN] = (
            stored.encode()
        )
        be.device.refresh(ch)
        be.write_channel(ch, _flat(gain_dbfs=-6.0))  # does not raise
        assert fake.writes

    def test_the_nudge_is_recorded_on_the_block(self, tmp_path):
        be, fake = _backend(tmp_path, PeqPolicy.EXCLUSIVE, nudge=True)
        ch, data_id = TestPlanIsAtomic.UNSENDABLE_AT
        stored = EqBand(freq=2514, level=480, bw=42, shf_db=0, type=0)
        fake.image.channels[ch][data_id * BLOCK_LEN : (data_id + 1) * BLOCK_LEN] = (
            stored.encode()
        )
        be.device.refresh(ch)
        plan = be.plan_channel(ch, _flat(gain_dbfs=-6.0))
        nudged = [b for b in plan if b.nudge]
        assert len(nudged) == 1
        assert nudged[0].data_id == data_id
        assert "2514->2515" in nudged[0].nudge

    def test_it_moves_the_minimum_distance(self, tmp_path):
        be, fake = _backend(tmp_path, PeqPolicy.EXCLUSIVE, nudge=True)
        ch, data_id = TestPlanIsAtomic.UNSENDABLE_AT
        stored = EqBand(freq=2514, level=480, bw=42, shf_db=0, type=0)
        fake.image.channels[ch][data_id * BLOCK_LEN : (data_id + 1) * BLOCK_LEN] = (
            stored.encode()
        )
        be.device.refresh(ch)
        plan = be.plan_channel(ch, _flat(gain_dbfs=-6.0))
        band = EqBand.decode(next(b.payload for b in plan if b.data_id == data_id))
        assert abs(band.freq - 2514) == 1

    def test_it_leaves_level_and_bandwidth_alone(self, tmp_path):
        # Bandwidth is genuinely quantised and level is the fit's output. The
        # nudge is allowed one field per block type, and for an EQ band that
        # field is frequency.
        be, fake = _backend(tmp_path, PeqPolicy.EXCLUSIVE, nudge=True)
        ch, data_id = TestPlanIsAtomic.UNSENDABLE_AT
        stored = EqBand(freq=2514, level=480, bw=42, shf_db=0, type=0)
        fake.image.channels[ch][data_id * BLOCK_LEN : (data_id + 1) * BLOCK_LEN] = (
            stored.encode()
        )
        be.device.refresh(ch)
        plan = be.plan_channel(ch, _flat(gain_dbfs=-6.0))
        band = EqBand.decode(next(b.payload for b in plan if b.data_id == data_id))
        assert band.bw == 42
        assert band.level == FLAT_LEVEL_RAW  # it is the flatten, unchanged in level

    def test_a_sendable_plan_is_left_completely_alone(self, tmp_path):
        be, _ = _backend(tmp_path, PeqPolicy.EXCLUSIVE, nudge=True)
        plan = be.plan_channel(0, _flat(gain_dbfs=-6.0))
        assert plan
        assert all(b.nudge is None for b in plan)

    def test_nudging_off_restores_the_refusal(self, tmp_path):
        be, fake = _backend(tmp_path, PeqPolicy.EXCLUSIVE, nudge=False)
        ch, data_id = TestPlanIsAtomic.UNSENDABLE_AT
        stored = EqBand(freq=2514, level=480, bw=42, shf_db=0, type=0)
        fake.image.channels[ch][data_id * BLOCK_LEN : (data_id + 1) * BLOCK_LEN] = (
            stored.encode()
        )
        be.device.refresh(ch)
        with pytest.raises(UnsendablePlan):
            be.write_channel(ch, _flat(gain_dbfs=-6.0))

    def test_a_misc_block_is_nudged_by_gain_not_delay(self):
        # One sample of delay at 48 kHz is 26 degrees of phase at 3.5 kHz --
        # a real change to the thing time alignment exists to control. Gain
        # moves in 0.1 dB steps, well under the 0.39 dB repeatability floor.
        misc = OutputMisc(
            enabled=1, polar=0, gain_raw=500, delay_raw=144, eq_mode=0, spk_type=1
        )
        variants = list(_nudged_variants(int(OutputBlock.MISC), misc.encode()))
        assert variants
        for payload, description in variants:
            got = OutputMisc.decode(payload)
            assert got.delay_raw == 144, description
            assert got.gain_raw != 500

    def test_the_zero_checksum_rate_is_what_the_docs_claim(self):
        # Pins the number the design rests on. If the checksum ever widens,
        # the "1 in 272" argument for nudging at all stops holding and this
        # says so.
        import random

        rng = random.Random(20260811)
        bad = 0
        trials = 20_000
        for _ in range(trials):
            payload = bytes(rng.randrange(256) for _ in range(8))
            frame = Frame(
                frame_type=FrameType.WRITE,
                data_type=DataType.OUTPUT_CHANNEL,
                channel_id=rng.randrange(8),
                data_id=rng.randrange(31),
                payload=payload,
                bluetooth_device_id=OBSERVED_BLUETOOTH_DEVICE_ID,
            )
            try:
                frame.encode()
            except UnsendableFrame:
                bad += 1
        # 1/256 expected; allow generous slack for 20k trials.
        assert 1 / 400 < bad / trials < 1 / 180


class TestTheInputMixer:
    """Block 33 decodes as the vendor app's mixer grid.

    Confirmed 2026-08-12 against screenshots of the Windows and iOS mixers:
    one byte per input, 0-100. The decode is cross-checked here against the
    live device's own records, and the reachability it reports matches
    `docs/hardware.md`'s table -- which was derived months earlier by sweeping
    outputs on the bench and listening for silence, sharing no reasoning with
    this decode.
    """

    #: OUT1..OUT8 from `snapshots/2026-08-11_predict.json`, block 33.
    LIVE = {
        0: (80, 0, 80, 0),
        1: (0, 80, 0, 80),
        2: (80, 0, 80, 0),
        3: (0, 80, 0, 80),
        4: (80, 0, 80, 0),
        5: (0, 80, 0, 80),
        6: (90, 90, 90, 90),
        7: (90, 90, 90, 90),
    }

    #: `docs/hardware.md`, derived on the bench by sweeping and listening for
    #: silence, long before block 33 was decoded.
    REACHABLE_FROM_INPUT_1 = (0, 2, 4, 6, 7)

    def test_the_live_records_decode_as_the_app_shows_them(self):
        import json
        from pathlib import Path

        blob = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "snapshots"
                / "2026-08-11_predict.json"
            ).read_text()
        )
        for output, expected in self.LIVE.items():
            record = bytes.fromhex(blob["channels"][output])
            mix = OutputMix.decode(record[33 * 8 : 34 * 8])
            assert mix.inputs == expected, f"OUT{output + 1}"

    def test_reachability_matches_the_bench_derived_table(self):
        import json
        from pathlib import Path

        blob = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "snapshots"
                / "2026-08-11_predict.json"
            ).read_text()
        )
        reachable = tuple(
            o
            for o in range(8)
            if OutputMix.decode(
                bytes.fromhex(blob["channels"][o])[33 * 8 : 34 * 8]
            ).reaches(0)
        )
        assert reachable == self.REACHABLE_FROM_INPUT_1

    def test_the_upper_four_slots_are_unused_on_this_model(self):
        # Eight slots on a four-input device. Zero everywhere, which is what
        # makes the shared-codebase reading of block 34's name plausible.
        import json
        from pathlib import Path

        blob = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "snapshots"
                / "2026-08-11_predict.json"
            ).read_text()
        )
        for output in range(8):
            record = bytes.fromhex(blob["channels"][output])
            mix = OutputMix.decode(record[33 * 8 : 34 * 8])
            assert mix.levels[4:] == (0, 0, 0, 0), f"OUT{output + 1}"

    def test_it_round_trips(self):
        raw = bytes((80, 0, 80, 0, 0, 0, 0, 0))
        assert OutputMix.decode(raw).encode() == raw

    def test_the_backend_reports_reachability(self, tmp_path):
        be, fake = _backend(tmp_path)
        fake.image.channels[0][33 * 8 : 34 * 8] = bytes((100, 0, 0, 0, 0, 0, 0, 0))
        fake.image.channels[1][33 * 8 : 34 * 8] = bytes((0, 100, 0, 0, 0, 0, 0, 0))
        be.device.refresh_all()
        assert be.input_mix(0).reaches(0)
        assert not be.input_mix(1).reaches(0)
        assert 0 in be.outputs_reachable_from(0)
        assert 1 not in be.outputs_reachable_from(0)

    def test_an_input_the_device_does_not_have_is_a_programming_error(self):
        with pytest.raises(ValueError):
            OutputMix.decode(bytes(8)).reaches(4)
