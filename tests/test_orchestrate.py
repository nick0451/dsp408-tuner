"""The closed tuning loop, rehearsed end to end with no hardware.

Two halves, tested differently.

**The decision logic** -- the objective, the plan, the freeze -- is pure and
gets ordinary unit tests.

**The run** is driven against the real stack: ``Dsp408Spp`` over
``Dsp408Device`` over a real session, framing and transmit policy, terminating
in :class:`~tuner.dsp.fake_device.FakeDsp408`. Nothing is mocked below the
transport, so these exercise the actual snapshot, preset-store, preset-recall,
journal and readback-verification paths. The only substitution is the
microphone, and :class:`SyntheticRig` is a real closed loop of its own: it
reads the device's current configuration and returns the response that
configuration would produce, so a correction the fitter writes genuinely
changes what the next sweep sees.

That last property is what makes an ACCEPTED verdict here mean something. A
rig returning canned curves would accept a tune that did nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import numpy as np
import pytest

from tuner.dsp.backend import Biquad, ChannelConfig, FilterType
from tuner.dsp.device import Dsp408Device, WriteJournal
from tuner.dsp.dsp408_spp import Dsp408Spp, PeqPolicy
from tuner.dsp.fake_device import FakeDsp408
from tuner.dsp.protocol import PRESET_SLOT_MAX
from tuner.dsp.session import Dsp408Session, Pacing
from tuner.dsp.transport import LoopbackTransport
from tuner.dsp.txpolicy import BlastRadius, TxPolicy
from tuner.measure.qa import SilentPath
from tuner.measure.result import Measurement, Provenance
from tuner.optimize import biquad
from tuner.optimize.verify import Outcome
from tuner.orchestrate import (
    IsolationError,
    MagnitudeObjective,
    MuteIsolator,
    NoIsolation,
    ObjectiveChanged,
    OrchestrationError,
    RollbackFailed,
    Stage,
    TunePlan,
    TuneRun,
)
from tuner.orchestrate.objective import ObjectiveError
from tuner.orchestrate.plan import DriverCeiling, Gang, PlanError
from tuner.orchestrate.run import power_average_db
from tuner.safety.limits import DEFAULT_CEILING_DBFS

SAMPLE_RATE_HZ = 48_000
AXIS = np.geomspace(30.0, 16_000.0, 200)


# ---------------------------------------------------------------------------
# Fixtures: a flat target, a device, and a rig that responds to it
# ---------------------------------------------------------------------------


def flat_target(name: str = "flat-test"):
    from tuner.optimize.target import flat

    return flat(np.array([20.0, 20_000.0]), name=name)


def an_objective(outputs=(0,), positions=1, **kwargs) -> MagnitudeObjective:
    return MagnitudeObjective(
        name=kwargs.pop("name", "test-flat-rms"),
        target=kwargs.pop("target", flat_target()),
        freqs_hz=AXIS,
        source_weights=dict.fromkeys(outputs, 1.0),
        position_weights=(1.0,) * positions,
        **kwargs,
    )


def a_plan(tmp_path, objective=None, **kwargs) -> TunePlan:
    return TunePlan(
        session_id=kwargs.pop("session_id", "s1"),
        objective=objective or an_objective(),
        scratch_slot=kwargs.pop("scratch_slot", 6),
        scratch_slot_confirmed_by=kwargs.pop(
            "scratch_slot_confirmed_by", "operator, bench, 2026-08-10"
        ),
        snapshot_path=kwargs.pop("snapshot_path", tmp_path / "baseline.json"),
        constraints=kwargs.pop(
            "constraints", replace(biquad.DEFAULT_CONSTRAINTS, max_bands=6)
        ),
        **kwargs,
    )


def a_backend(tmp_path, policy: PeqPolicy = PeqPolicy.EXCLUSIVE) -> Dsp408Spp:
    fake = FakeDsp408()
    session = Dsp408Session(
        LoopbackTransport(fake),
        policy=TxPolicy(
            allow_writes=True,
            allow_presets=True,
            blast_radius=BlastRadius(max_writes=4000, max_channels=8),
        ),
        pacing=Pacing(idle_after_reply_s=0.0, max_requests_per_s=1e9),
    )
    device = Dsp408Device(
        session, journal=WriteJournal(tmp_path / "journal.jsonl"), session_id="s1"
    )
    backend = Dsp408Spp(device=device, peq_policy=policy)
    backend.connect()
    return backend


def a_run(plan, backend, measurer, isolator=None) -> TuneRun:
    """A run with the real mute isolator unless a test wants otherwise.

    The fake device's records carry no link groups, so ``MuteIsolator``
    exercises its whole path here -- write, mirror, read back, prove silence,
    restore -- against real framing and a real journal.
    """
    return TuneRun(plan, backend, measurer, isolator or MuteIsolator(backend))


#: A speaker with two problems the fitter can actually correct: a broad
#: midrange suck-out and a narrow peak. Both are inside the scored band.
def speaker_db(freqs_hz: np.ndarray) -> np.ndarray:
    f = np.maximum(np.asarray(freqs_hz, dtype=np.float64), 1.0)
    dip = -6.0 * np.exp(-(((np.log2(f / 300.0)) / 1.2) ** 2))
    peak = 5.0 * np.exp(-(((np.log2(f / 3_000.0)) / 0.35) ** 2))
    return dip + peak


def impulse_from(mag_db: np.ndarray, n: int = 16_384) -> np.ndarray:
    """A zero-phase impulse whose magnitude is ``mag_db`` on the rfft grid.

    Zero phase is fine because the objective is magnitude-only. It also makes
    the rig deterministic, which is what lets a test assert an exact floor.
    """
    return np.fft.irfft(10.0 ** (mag_db / 20.0), n)


@dataclass
class SyntheticRig:
    """A measurer that reads the DSP and returns what it would sound like.

    The loop is genuine: ``measure`` reads the live channel configuration
    through the backend, evaluates its biquad chain, adds the fixed speaker
    anomaly, and returns that. A correction the run writes is therefore
    audible to the next sweep, and a run that accepts has actually improved
    something.

    ``noise_db`` is a deterministic per-call perturbation, so the session's
    repeatability floor is non-zero and the invariant has something to clear.
    """

    backend: Dsp408Spp
    noise_db: float = 0.02
    temperature_c: float = 21.0
    setup_token: str | None = "synthetic rig, unmoved"
    gains_db: tuple[float, ...] = (30.0,)
    device_name: str = "synthetic rig"
    n: int = 16_384
    calls: list[tuple[int, float, str]] = field(default_factory=list)
    _seed: int = 0

    def response_db(self, output: int, freqs_hz: np.ndarray) -> np.ndarray:
        config = self.backend.read_channel(output)
        eq = biquad.response_db(tuple(config.peq), freqs_hz, SAMPLE_RATE_HZ)
        return speaker_db(freqs_hz) + eq

    def measure(self, output, limit, tag):
        self.calls.append((output, limit.ceiling_dbfs, tag))
        # A muted output makes no sound, and capture_sweep's safety ramp
        # raises SilentPath when the stimulus does not arrive. Modelling that
        # here is what lets prove_silence be tested for real rather than
        # stubbed: the proof is the existing check, inverted.
        if self.backend.read_channel(output).muted:
            raise SilentPath(f"output {output} is muted")
        freqs = np.fft.rfftfreq(self.n, 1.0 / SAMPLE_RATE_HZ)
        mag = self.response_db(output, freqs)
        self._seed += 1
        rng = np.random.default_rng(self._seed)
        mag = mag + rng.normal(0.0, self.noise_db, mag.shape)
        return [
            Measurement(
                impulse=impulse_from(mag, self.n),
                provenance=Provenance(
                    device=self.device_name,
                    sample_rate_hz=SAMPLE_RATE_HZ,
                    gains_db=self.gains_db,
                    timestamp=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
                    temperature_c=self.temperature_c,
                    setup_token=self.setup_token,
                ),
            )
        ]


@dataclass
class DeafRig(SyntheticRig):
    """A rig the DSP cannot influence: EQ is written and nothing changes.

    This is the failure the improvement invariant exists to catch -- the tune
    is applied, the model predicts an improvement, and the system does not
    move. Physically it is a misrouted channel, a muted output, or a
    measurement pointed at the wrong device.
    """

    def response_db(self, output: int, freqs_hz: np.ndarray) -> np.ndarray:
        return speaker_db(freqs_hz)


# ---------------------------------------------------------------------------
# The objective
# ---------------------------------------------------------------------------


class TestObjective:
    def test_a_flat_response_against_a_flat_target_scores_zero(self):
        obj = an_objective()
        freqs = np.fft.rfftfreq(16_384, 1.0 / SAMPLE_RATE_HZ)
        flat = Measurement(
            impulse=impulse_from(np.zeros_like(freqs)),
            provenance=_provenance(),
        )
        assert obj.score_one(flat) == pytest.approx(0.0, abs=0.02)

    def test_deviation_is_measured_against_the_level_matched_target(self):
        # A response 10 dB hot everywhere is a gain error, not a shape error,
        # and the objective is deliberately blind to it -- see the module
        # docstring in orchestrate/objective.py.
        obj = an_objective()
        freqs = np.fft.rfftfreq(16_384, 1.0 / SAMPLE_RATE_HZ)
        hot = Measurement(
            impulse=impulse_from(np.full_like(freqs, 10.0)),
            provenance=_provenance(),
        )
        assert obj.score_one(hot) == pytest.approx(0.0, abs=0.02)

    def test_a_bumpy_response_scores_worse_than_a_smooth_one(self):
        obj = an_objective()
        freqs = np.fft.rfftfreq(16_384, 1.0 / SAMPLE_RATE_HZ)
        bumpy = Measurement(
            impulse=impulse_from(speaker_db(freqs)), provenance=_provenance()
        )
        smooth = Measurement(
            impulse=impulse_from(0.25 * speaker_db(freqs)), provenance=_provenance()
        )
        assert obj.score_one(bumpy) > obj.score_one(smooth) > 0.0

    def test_positions_combine_on_power_not_on_rms(self):
        # Averaging rms would report sqrt((0+16)/2)=2.83 as if it were the
        # same as two seats at 2.83. Mean-square-then-root gives 2.83 here and
        # 2.83 there, but the point is the ordering: one terrible seat must
        # not be diluted by one perfect one below what two mediocre seats
        # would score.
        obj = an_objective(positions=2)
        freqs = np.fft.rfftfreq(16_384, 1.0 / SAMPLE_RATE_HZ)
        perfect = Measurement(
            impulse=impulse_from(np.zeros_like(freqs)), provenance=_provenance()
        )
        awful = Measurement(
            impulse=impulse_from(4.0 * np.sign(np.sin(freqs / 200.0))),
            provenance=_provenance(),
        )
        mediocre_pair = obj.score({0: [awful, awful]})
        split = obj.score({0: [perfect, awful]})
        assert split == pytest.approx(mediocre_pair / np.sqrt(2), rel=0.05)

    def test_missing_source_is_an_error_not_a_silent_skip(self):
        obj = an_objective(outputs=(0, 1))
        with pytest.raises(ObjectiveError, match=r"no measurement was given"):
            obj.score({0: [_flat_measurement()]})

    def test_unweighted_source_is_refused(self):
        obj = an_objective(outputs=(0,))
        with pytest.raises(ObjectiveError, match="unweighted sources"):
            obj.score({0: [_flat_measurement()], 3: [_flat_measurement()]})

    def test_wrong_position_count_is_an_error(self):
        obj = an_objective(positions=2)
        with pytest.raises(ObjectiveError, match="weights 2 positions"):
            obj.score({0: [_flat_measurement()]})

    def test_a_band_containing_no_axis_points_is_refused(self):
        with pytest.raises(ObjectiveError, match="contains none of the"):
            an_objective(band_hz=(17_000.0, 19_000.0))

    def test_the_axis_is_frozen_against_in_place_edits(self):
        # An objective whose frequency axis can be mutated is not frozen, and
        # the fingerprint would not notice.
        obj = an_objective()
        with pytest.raises(ValueError, match="read-only"):
            obj.freqs_hz[0] = 1.0


class TestTheFreeze:
    def test_the_same_objective_hashes_the_same(self):
        assert an_objective().fingerprint() == an_objective().fingerprint()

    def test_reweighting_the_seats_changes_the_hash(self):
        # The exact move the invariant's first qualification exists to stop.
        a = an_objective(positions=2)
        b = replace(a, position_weights=(3.0, 1.0))
        assert a.fingerprint() != b.fingerprint()

    def test_editing_the_target_curve_changes_the_hash(self):
        from tuner.optimize.target import tilted

        a = an_objective()
        b = an_objective(
            target=tilted(np.array([20.0, 20_000.0]), tilt_db_per_decade=-1.0)
        )
        assert a.fingerprint() != b.fingerprint()

    def test_renaming_changes_the_hash(self):
        assert an_objective().fingerprint() != an_objective(name="other").fingerprint()

    def test_require_unchanged_passes_on_the_same_objective(self):
        obj = an_objective()
        obj.require_unchanged(obj.fingerprint())

    def test_require_unchanged_names_both_hashes(self):
        obj = an_objective()
        stale = an_objective(name="before").fingerprint()
        with pytest.raises(ObjectiveChanged) as exc:
            obj.require_unchanged(stale)
        assert stale[:12] in str(exc.value)
        assert obj.fingerprint()[:12] in str(exc.value)
        assert "do not report this comparison" in str(exc.value)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


class TestPlan:
    def test_a_valid_plan_constructs(self, tmp_path):
        plan = a_plan(tmp_path)
        assert plan.outputs == (0,)
        assert plan.fingerprint()

    def test_the_working_area_is_not_a_backup_slot(self, tmp_path):
        with pytest.raises(PlanError, match="live working area"):
            a_plan(tmp_path, scratch_slot=0)

    def test_a_slot_beyond_the_device_is_refused(self, tmp_path):
        # Six slots. "Fifteen" was an inference from the app's name reads and
        # it was wrong; this is the test that stops it coming back.
        with pytest.raises(PlanError, match=f"1..{PRESET_SLOT_MAX}"):
            a_plan(tmp_path, scratch_slot=15)

    def test_an_unconfirmed_slot_is_refused(self, tmp_path):
        with pytest.raises(PlanError, match="confirmed expendable"):
            a_plan(tmp_path, scratch_slot_confirmed_by="   ")

    def test_one_repeat_cannot_establish_a_floor(self, tmp_path):
        with pytest.raises(PlanError, match="at least 2 repeats"):
            a_plan(tmp_path, floor_repeats=1)

    def test_outputs_come_from_the_objective(self, tmp_path):
        plan = a_plan(tmp_path, objective=an_objective(outputs=(0, 3, 5)))
        assert plan.outputs == (0, 3, 5)

    def test_an_output_outside_the_device_is_refused(self, tmp_path):
        with pytest.raises(PlanError, match="outside 0..7"):
            a_plan(tmp_path, objective=an_objective(outputs=(9,)))

    def test_an_unlisted_output_gets_the_tweeter_safe_default(self, tmp_path):
        ceiling, characterized = a_plan(tmp_path).ceiling_for(0)
        assert ceiling == DEFAULT_CEILING_DBFS
        assert characterized is False

    def test_a_raised_ceiling_needs_its_basis_in_writing(self):
        with pytest.raises(PlanError, match="needs a basis"):
            DriverCeiling(ceiling_dbfs=-6.0, basis="  ")

    def test_a_bare_number_is_not_a_ceiling(self, tmp_path):
        with pytest.raises(PlanError, match="the basis is the point"):
            a_plan(tmp_path, driver_ceilings={0: -6.0})

    def test_a_documented_ceiling_is_honoured(self, tmp_path):
        plan = a_plan(
            tmp_path,
            driver_ceilings={
                0: DriverCeiling(-6.0, "Focal 6.5in mid, 4th-order HP at 80 Hz, 80 W")
            },
        )
        assert plan.ceiling_for(0) == (-6.0, True)

    def test_the_basis_reaches_provenance(self, tmp_path):
        plan = a_plan(
            tmp_path, driver_ceilings={0: DriverCeiling(-6.0, "known 6.5in mid")}
        )
        assert plan.as_dict()["driver_ceilings"]["0"]["basis"] == "known 6.5in mid"

    def test_the_slot_confirmation_reaches_provenance(self, tmp_path):
        assert (
            a_plan(tmp_path).as_dict()["scratch_slot_confirmed_by"]
            == "operator, bench, 2026-08-10"
        )

    def test_an_unnamed_slot_records_the_weaker_claim(self, tmp_path):
        assert a_plan(tmp_path).as_dict()["scratch_slot_holds"] is None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


class TestAnAcceptedRun:
    def test_a_correctable_speaker_is_corrected_and_accepted(self, tmp_path):
        backend = a_backend(tmp_path)
        rig = SyntheticRig(backend)
        report = a_run(a_plan(tmp_path), backend, rig).execute()

        assert report.outcome is Outcome.ACCEPTED, report.summary()
        assert report.verdict.result_score < report.verdict.baseline_score
        assert report.verdict.delta < -report.floor.value

    def test_the_device_holds_the_fitted_bands_afterwards(self, tmp_path):
        backend = a_backend(tmp_path)
        report = a_run(a_plan(tmp_path), backend, SyntheticRig(backend)).execute()
        assert report.accepted
        assert len(backend.read_channel(0).peq) >= 1

    def test_every_stage_ran_and_is_recorded(self, tmp_path):
        backend = a_backend(tmp_path)
        report = a_run(a_plan(tmp_path), backend, SyntheticRig(backend)).execute()
        for stage in Stage:
            assert report.stage(stage) is not None, f"{stage} missing"

    def test_the_baseline_is_on_disk_and_in_the_slot_before_any_write(self, tmp_path):
        backend = a_backend(tmp_path)
        plan = a_plan(tmp_path)
        report = a_run(plan, backend, SyntheticRig(backend)).execute()
        assert plan.snapshot_path.exists()
        arm = report.stage(Stage.ARM)
        assert arm.data["scratch_slot"] == plan.scratch_slot
        # ARM is recorded before WRITE, which is the ordering that matters.
        stages = [s.stage for s in report.stages]
        assert stages.index(Stage.ARM) < stages.index(Stage.WRITE)

    def test_the_report_is_a_provenance_record(self, tmp_path):
        import json

        backend = a_backend(tmp_path)
        plan = a_plan(tmp_path)
        report = a_run(plan, backend, SyntheticRig(backend)).execute()
        blob = json.loads(report.to_json())
        assert blob["objective_fingerprint"] == plan.objective.fingerprint()
        assert blob["plan"]["scratch_slot_confirmed_by"]
        assert blob["floor"]["n_repeats"] == plan.floor_repeats
        assert blob["floor"]["session_id"] == plan.session_id
        assert blob["verdict"]["outcome"] == "accepted"

    def test_writes_are_disarmed_once_the_run_settles(self, tmp_path):
        backend = a_backend(tmp_path)
        a_run(a_plan(tmp_path), backend, SyntheticRig(backend)).execute()
        assert backend.device.armed is False

    def test_the_device_is_written_once_not_per_iteration(self, tmp_path):
        # "Fit offline, write once per round." A fitter that wrote a candidate
        # per iteration would spend tens of thousands of non-volatile writes
        # on states nobody measures.
        backend = a_backend(tmp_path)
        plan = a_plan(tmp_path)
        report = a_run(plan, backend, SyntheticRig(backend)).execute()
        # 6 bands + misc + xover, flattening aside: comfortably under 40.
        assert report.stage(Stage.WRITE).data["block_writes"] < 40


class TestARejectedRun:
    def test_a_tune_that_changes_nothing_is_rejected(self, tmp_path):
        backend = a_backend(tmp_path)
        report = a_run(a_plan(tmp_path), backend, DeafRig(backend)).execute()
        assert report.outcome is Outcome.REJECTED, report.summary()
        assert "does not exceed" in report.verdict.reason

    def test_rejection_rolls_the_device_back(self, tmp_path):
        backend = a_backend(tmp_path)
        before = backend.record(0)
        report = a_run(a_plan(tmp_path), backend, DeafRig(backend)).execute()
        assert report.verdict.requires_rollback
        assert report.rollback.device_matches
        assert backend.device.refresh(0) == before

    def test_the_rollback_is_verified_by_re_measurement(self, tmp_path):
        # Byte-equality proves the settings went back. Only a sweep proves the
        # system did.
        backend = a_backend(tmp_path)
        report = a_run(a_plan(tmp_path), backend, DeafRig(backend)).execute()
        assert report.rollback_verified_acoustically is True
        settle = report.stage(Stage.SETTLE)
        assert "re-measured" in settle.detail
        assert settle.data["drift_db"] <= report.floor.value

    def test_a_rollback_the_ear_disagrees_with_raises(self, tmp_path):
        # The bytes go back, and the system does not. On a real rig this is a
        # microphone that moved, a door that opened, or a cabin that warmed --
        # and reporting it as a clean rollback is how a run silently leaves
        # the car changed.
        backend = a_backend(tmp_path)
        rig = DeafRig(backend)
        run = a_run(a_plan(tmp_path), backend, rig)

        original = rig.response_db

        def drifting(output, freqs_hz):
            # Only the post-rollback sweep is affected.
            if rig.calls and rig.calls[-1][2] == "rollback":
                return original(output, freqs_hz) * 2.0
            return original(output, freqs_hz)

        rig.response_db = drifting
        with pytest.raises(RollbackFailed, match="did not come back"):
            run.execute()

    def test_the_rollback_uses_the_preset_path_first(self, tmp_path):
        backend = a_backend(tmp_path)
        report = a_run(a_plan(tmp_path), backend, DeafRig(backend)).execute()
        assert report.stage(Stage.SETTLE).data["restore_path"].startswith("preset")


class TestIndeterminate:
    def test_drifting_temperature_is_indeterminate_not_a_failure(self, tmp_path):
        backend = a_backend(tmp_path)
        rig = SyntheticRig(backend)
        run = a_run(a_plan(tmp_path), backend, rig)

        real_measure = rig.measure

        def warming(output, limit, tag):
            if tag == "verify":
                rig.temperature_c = 40.0
            return real_measure(output, limit, tag)

        rig.measure = warming
        report = run.execute()
        assert report.outcome is Outcome.INDETERMINATE, report.summary()
        # Name the term, not the category. "Incomparable provenance" leaves
        # the operator to guess which of six fields moved, on a verdict that
        # arrives after the device has already been changed and restored.
        assert "temperature moved 19.0 K" in report.verdict.reason
        assert report.rollback is not None

    def test_a_moved_master_volume_is_indeterminate(self, tmp_path):
        # The round-3 finding, wired in: master volume was *captured* in the
        # snapshot and compared by nothing, so a global that moved between
        # baseline and verification would have been attributed to the tune.
        backend = a_backend(tmp_path)
        rig = SyntheticRig(backend)
        run = a_run(a_plan(tmp_path), backend, rig)

        real_measure = rig.measure
        fake = backend.device.session.transport.device

        def nudge_the_volume(output, limit, tag):
            if tag == "verify":
                block = bytearray(fake.image.system[5])
                block[0] = (block[0] + 7) % 256
                fake.image.system[5] = bytes(block)
            return real_measure(output, limit, tag)

        rig.measure = nudge_the_volume
        report = run.execute()
        assert report.outcome is Outcome.INDETERMINATE, report.summary()
        assert "master volume" in report.verdict.reason

    def test_indeterminate_still_rolls_back(self, tmp_path):
        backend = a_backend(tmp_path)
        before = backend.record(0)
        rig = SyntheticRig(backend)
        run = a_run(a_plan(tmp_path), backend, rig)
        real_measure = rig.measure

        def warming(output, limit, tag):
            if tag == "verify":
                rig.temperature_c = 40.0
            return real_measure(output, limit, tag)

        rig.measure = warming
        run.execute()
        assert backend.device.refresh(0) == before


class TestTheFreezeIsEnforcedDuringARun:
    def test_reweighting_mid_run_voids_the_verdict(self, tmp_path):
        backend = a_backend(tmp_path)
        rig = SyntheticRig(backend)
        run = a_run(a_plan(tmp_path, objective=an_objective(positions=1)), backend, rig)

        real_measure = rig.measure

        def swap_the_objective(output, limit, tag):
            if tag == "verify":
                object.__setattr__(run.plan.objective, "name", "reweighted")
            return real_measure(output, limit, tag)

        rig.measure = swap_the_objective
        report = run.execute()
        assert report.verdict is None
        assert "ObjectiveChanged" in report.error
        # And the run still put the car back.
        assert report.rollback.device_matches


class TestSafetyRuleSix:
    def test_every_sweep_gets_a_ceiling_from_live_device_state(self, tmp_path):
        backend = a_backend(tmp_path)
        rig = SyntheticRig(backend)
        a_run(a_plan(tmp_path), backend, rig).execute()
        assert rig.calls
        assert all(ceiling <= DEFAULT_CEILING_DBFS for _, ceiling, _ in rig.calls)

    def test_a_boost_written_by_the_run_lowers_the_next_sweep(self, tmp_path):
        # The exact sequence rule 6 exists for: the optimizer writes a boost
        # and then immediately sweeps the channel it just boosted, with no
        # human in between to remember to turn the level down.
        backend = a_backend(tmp_path)
        backend.device.arm_writes(reason="test", evidence=_evidence(backend, tmp_path))

        # Start from a known 0 dB channel with no bands, so the only thing
        # moving is the boost. (Channel gain nets against it, which is right
        # and is why the fake's stock -10 dB gain would have masked +9 dB.)
        backend.write_channel(
            0, replace(backend.read_channel(0), gain_dbfs=0.0, peq=())
        )
        before = backend.stimulus_limit(0).ceiling_dbfs
        assert before == DEFAULT_CEILING_DBFS

        backend.write_channel(
            0,
            replace(
                backend.read_channel(0),
                gain_dbfs=0.0,
                peq=(Biquad(1_000.0, 9.0, 2.0, FilterType.PEAKING),),
            ),
        )
        after = backend.stimulus_limit(0).ceiling_dbfs
        assert after == pytest.approx(before - 9.0, abs=0.15)

    def test_channel_gain_and_boost_net_against_each_other(self, tmp_path):
        # A +9 dB band on a channel trimmed to -10 dB is 1 dB of attenuation
        # at the speaker, not 9 dB of boost. Subtracting the boost alone would
        # be pessimistic by the trim -- harmless, but it would make every
        # sweep on a trimmed channel quieter than it needs to be, and a
        # too-quiet sweep is a noisier measurement.
        backend = a_backend(tmp_path)
        backend.device.arm_writes(reason="test", evidence=_evidence(backend, tmp_path))
        backend.write_channel(
            0,
            replace(
                backend.read_channel(0),
                gain_dbfs=-10.0,
                peq=(Biquad(1_000.0, 9.0, 2.0, FilterType.PEAKING),),
            ),
        )
        assert backend.stimulus_limit(0).ceiling_dbfs == DEFAULT_CEILING_DBFS

    def test_a_documented_driver_ceiling_reaches_the_rig(self, tmp_path):
        backend = a_backend(tmp_path)
        rig = SyntheticRig(backend)
        plan = a_plan(
            tmp_path,
            driver_ceilings={0: DriverCeiling(-8.0, "bench, nothing connected")},
        )
        a_run(plan, backend, rig).execute()
        # -8 dB driver ceiling, less whatever the channel's own gain adds.
        assert max(c for _, c, _ in rig.calls) <= -8.0
        assert max(c for _, c, _ in rig.calls) > DEFAULT_CEILING_DBFS


class TestAborts:
    def test_a_measurement_failure_rolls_back_without_claiming_an_ear_check(
        self, tmp_path
    ):
        backend = a_backend(tmp_path)
        before = backend.record(0)
        rig = SyntheticRig(backend)
        run = a_run(a_plan(tmp_path), backend, rig)

        real_measure = rig.measure

        def die_on_verify(output, limit, tag):
            if tag == "verify":
                raise OSError("the interface went away")
            return real_measure(output, limit, tag)

        rig.measure = die_on_verify
        report = run.execute()

        assert report.verdict is None
        assert "the interface went away" in report.error
        assert backend.device.refresh(0) == before
        # None, not False: an abort can *be* the measurement path failing, so
        # a re-measurement would not be evidence of anything.
        assert report.rollback_verified_acoustically is None

    def test_a_failure_before_arming_leaves_nothing_to_undo(self, tmp_path):
        # No handshake, so there is no identity to stamp into the snapshot's
        # provenance and ARM fails on its first line. Nothing was stored, so
        # there is nothing to restore -- and attempting a rollback here would
        # recall an empty slot over a live tune.
        backend = a_backend(tmp_path)
        backend._identity = None
        report = a_run(a_plan(tmp_path), backend, SyntheticRig(backend)).execute()
        assert report.rollback is None
        assert "not connected" in report.error
        assert not (tmp_path / "baseline.json").exists()

    def test_a_floor_from_another_session_is_refused(self, tmp_path):
        # The floor moves with temperature, mounting and ambient noise. Any
        # path that lets a previous session's number through is a path to
        # accepting noise.
        backend = a_backend(tmp_path)
        rig = SyntheticRig(backend)
        run = a_run(a_plan(tmp_path), backend, rig)
        original = run._close_floor

        def stale_floor():
            return replace(original(), session_id="yesterday")

        run._close_floor = stale_floor
        report = run.execute()
        assert "never be inherited" in report.error


class TestRefusals:
    def test_a_run_needs_the_exclusive_peq_policy(self, tmp_path):
        backend = a_backend(tmp_path, policy=PeqPolicy.LEADING)
        with pytest.raises(OrchestrationError, match="EXCLUSIVE"):
            a_run(a_plan(tmp_path), backend, SyntheticRig(backend))

    def test_delay_alignment_without_a_loopback_stops_the_run(self, tmp_path):
        # The synthetic rig has no timing reference, which is also true of the
        # Scarlett Solo. Skipping the step quietly and reporting a
        # magnitude-only tune as aligned is the failure being prevented.
        backend = a_backend(tmp_path)
        plan = a_plan(tmp_path, align_delays=True)
        report = a_run(plan, backend, SyntheticRig(backend)).execute()
        assert "NoTimingReference" in report.error
        assert report.rollback.device_matches


class TestPowerAveraging:
    def test_two_identical_curves_average_to_themselves(self):
        curve = np.array([0.0, -3.0, 6.0])
        got = power_average_db([curve, curve], [1.0, 1.0])
        assert got == pytest.approx(curve)

    def test_averaging_is_on_power_not_on_decibels(self):
        # 0 dB and -20 dB: power mean is (1 + 0.01)/2 = 0.505 -> -2.97 dB.
        # A mean of dB values would say -10 dB, which understates the loud
        # seat by 7 dB and is the reason this is not a np.mean.
        got = power_average_db([np.array([0.0]), np.array([-20.0])], [1.0, 1.0])
        assert got[0] == pytest.approx(-2.966, abs=0.01)

    def test_weights_bias_the_average(self):
        heavy = power_average_db([np.array([0.0]), np.array([-20.0])], [9.0, 1.0])
        assert heavy[0] > -1.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _provenance(**kwargs) -> Provenance:
    return Provenance(
        device=kwargs.pop("device", "synthetic rig"),
        sample_rate_hz=SAMPLE_RATE_HZ,
        gains_db=(30.0,),
        timestamp=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        temperature_c=kwargs.pop("temperature_c", 21.0),
        setup_token=kwargs.pop("setup_token", "synthetic rig, unmoved"),
        **kwargs,
    )


def _flat_measurement() -> Measurement:
    freqs = np.fft.rfftfreq(4096, 1.0 / SAMPLE_RATE_HZ)
    return Measurement(
        impulse=impulse_from(np.zeros_like(freqs), 4096), provenance=_provenance()
    )


def _evidence(backend: Dsp408Spp, tmp_path):
    from tuner.dsp import snapshot as snap

    shot = snap.capture(backend.device, backend.identity)
    return shot.save(tmp_path / "arm.json")


# ---------------------------------------------------------------------------
# The acoustic measurer. Never run against a rig; these pin its refusals and
# the one thing it must not get wrong, which is honouring the run's ceiling.
# ---------------------------------------------------------------------------


class TestAcousticMeasurer:
    def _rig(self, monkeypatch, **kwargs):
        from tuner.measure.capture import CaptureConfig, SessionInfo
        from tuner.orchestrate import rig as rig_module

        captured: list = []

        def fake_capture_sweep(config, session, now=None):
            captured.append((config, session))
            return {1: _flat_measurement()}

        monkeypatch.setattr(rig_module, "capture_sweep", fake_capture_sweep)
        kwargs.setdefault("linearity", _linear_result())
        measurer = rig_module.AcousticMeasurer(
            config=CaptureConfig(sample_rate_hz=SAMPLE_RATE_HZ, input_channels=(1,)),
            session=SessionInfo(
                gains_db=(30.0,), setup_token="driver seat, mic at headrest"
            ),
            positions=kwargs.pop("positions", ("driver",)),
            **kwargs,
        )
        return measurer, captured

    def test_the_runs_ceiling_overrides_the_template(self, monkeypatch):
        # The template's own limit and level are ignored rather than merged.
        # Merging would let a stale template raise a ceiling the run had just
        # lowered because it wrote a boost.
        from tuner.safety.limits import ChannelLimit

        measurer, captured = self._rig(monkeypatch, headroom_db=3.0)
        measurer.measure(0, ChannelLimit(-26.0, characterized=False), "baseline")
        config, _ = captured[0]
        assert config.limit.ceiling_dbfs == -26.0
        assert config.level_dbfs == -29.0

    def test_a_run_with_no_linearity_result_is_refused(self, monkeypatch):
        from tuner.orchestrate.rig import RigError
        from tuner.safety.limits import ChannelLimit

        measurer, _ = self._rig(monkeypatch, linearity=None)
        with pytest.raises(RigError, match="unverified"):
            measurer.measure(0, ChannelLimit(), "baseline")

    def test_linearity_is_checked_once_not_per_sweep(self, monkeypatch):
        from tuner.orchestrate import rig as rig_module
        from tuner.safety.limits import ChannelLimit

        calls = []
        monkeypatch.setattr(
            rig_module, "require_linear_path", lambda r: calls.append(r)
        )
        measurer, _ = self._rig(monkeypatch)
        for _ in range(3):
            measurer.measure(0, ChannelLimit(), "floor")
        assert len(calls) == 1

    def test_several_seats_without_a_way_to_move_the_microphone_is_refused(
        self, monkeypatch
    ):
        from tuner.orchestrate.rig import RigError

        with pytest.raises(RigError, match="measure the same seat"):
            self._rig(monkeypatch, positions=("driver", "passenger"))

    def test_two_input_channels_is_an_error_not_a_guess(self, monkeypatch):
        from tuner.measure.capture import CaptureConfig, SessionInfo
        from tuner.orchestrate import rig as rig_module
        from tuner.safety.limits import ChannelLimit

        monkeypatch.setattr(
            rig_module,
            "capture_sweep",
            lambda c, s, now=None: {1: _flat_measurement(), 2: _flat_measurement()},
        )
        measurer = rig_module.AcousticMeasurer(
            config=CaptureConfig(sample_rate_hz=SAMPLE_RATE_HZ, input_channels=(1, 2)),
            session=SessionInfo(
                gains_db=(30.0, 30.0), setup_token="driver seat, mic at headrest"
            ),
            positions=("driver",),
            linearity=_linear_result(),
        )
        with pytest.raises(rig_module.RigError, match="would be a guess"):
            measurer.measure(0, ChannelLimit(), "baseline")

    def test_negative_headroom_would_sweep_above_the_ceiling(self, monkeypatch):
        from tuner.orchestrate.rig import RigError

        with pytest.raises(RigError, match="above the ceiling"):
            self._rig(monkeypatch, headroom_db=-1.0)

    def test_the_position_reaches_provenance_notes(self, monkeypatch):
        from tuner.safety.limits import ChannelLimit

        measurer, captured = self._rig(monkeypatch)
        measurer.measure(5, ChannelLimit(), "verify")
        _, session = captured[0]
        assert session.notes["position"] == "driver"
        assert session.notes["dsp_output"] == "5"
        assert session.notes["tag"] == "verify"


def _linear_result():
    from tuner.measure.qa import LinearityResult

    return LinearityResult(
        freqs_hz=(200.0, 1_000.0, 4_000.0),
        levels_dbfs=(-30.0, -24.0, -18.0),
        gain_db=np.zeros((3, 3)),
    )


# ---------------------------------------------------------------------------
# Isolation. The operator's manual method -- mute 2-8, check the app, sweep --
# automated, with the two things that change when nobody is watching.
# ---------------------------------------------------------------------------


class TestTheScratchSlot:
    """Slot 6 is the operator's designated scratch slot, confirmed 2026-08-10.

    The plan still refuses to default to it. What is recorded here is the
    *guard* the designation made worth having, not the number.
    """

    def test_the_slot_the_device_is_running_from_is_refused(self, tmp_path):
        # That slot is the operator's manual fallback -- recalling it is how a
        # person puts the car back when everything we built has failed.
        # Storing our baseline over it removes that, and the run would still
        # look correct, because it has two other restore paths of its own.
        backend = a_backend(tmp_path)
        live = backend.identity.current_preset
        report = a_run(
            a_plan(tmp_path, scratch_slot=live), backend, SyntheticRig(backend)
        ).execute()
        assert "currently running from" in report.error
        assert report.rollback is None

    def test_nothing_was_stored_before_the_refusal(self, tmp_path):
        # The guard has to fire before the store, not after it -- afterwards
        # is the one time it cannot help.
        backend = a_backend(tmp_path)
        live = backend.identity.current_preset
        slots = backend.device.session.transport.device.image.presets
        before = slots[live - 1]
        a_run(
            a_plan(tmp_path, scratch_slot=live), backend, SyntheticRig(backend)
        ).execute()
        assert slots[live - 1] == before

    def test_a_different_slot_is_allowed(self, tmp_path):
        backend = a_backend(tmp_path)
        assert backend.identity.current_preset != 6
        report = a_run(
            a_plan(tmp_path, scratch_slot=6), backend, SyntheticRig(backend)
        ).execute()
        assert report.accepted


class TestIsolation:
    def test_muting_everything_and_sweeping_is_the_proof(self, tmp_path):
        backend = a_backend(tmp_path)
        isolator = _proved(backend, SyntheticRig(backend), tmp_path)
        assert isolator.describe()["proved_silent"] is True

    def test_a_path_mute_does_not_silence_is_caught(self, tmp_path):
        # Every output muted and the microphone still hears the stimulus.
        # Physically: a path around the DSP, a driver fed by an output we are
        # not managing, or `enabled` not meaning on this unit what the A/B
        # said. A readback cannot see any of those; a sweep can.
        backend = a_backend(tmp_path)
        shot = _snapshot_of(backend)
        backend.device.arm_writes(
            reason="test", evidence=shot.save(tmp_path / "e.json")
        )
        isolator = MuteIsolator(backend)
        isolator.begin(shot)
        with pytest.raises(IsolationError, match="still hears the stimulus"):
            isolator.prove_silence(lambda: None)

    def test_isolate_refuses_until_silence_has_been_proved(self, tmp_path):
        backend = a_backend(tmp_path)
        isolator = MuteIsolator(backend)
        isolator.begin(_snapshot_of(backend))
        with pytest.raises(IsolationError, match="not been proved by measurement"):
            isolator.isolate([0])

    def test_a_linked_pair_cannot_be_isolated_from_itself(self, tmp_path):
        # The vendor app keeps a linked pair consistent by writing both, so
        # muting one half either mutes both or leaves the device disagreeing
        # with the model. The fix is unlinking in the app, and it has to
        # happen before the baseline snapshot.
        backend = a_backend(tmp_path)
        fake = backend.device.session.transport.device
        for channel in (6, 7):
            record = bytearray(fake.image.channels[channel])
            record[35 * 8 + 7] = 3
            fake.image.channels[channel] = record
        with pytest.raises(IsolationError, match=r"\[6, 7\] are in a link group"):
            MuteIsolator(backend).begin(_snapshot_of(backend))

    def test_isolation_leaves_exactly_one_output_audible(self, tmp_path):
        backend = a_backend(tmp_path)
        isolator = _proved(backend, SyntheticRig(backend), tmp_path)
        isolator.isolate([3])
        muted = {ch: backend.read_channel(ch).muted for ch in range(8)}
        assert muted[3] is False
        assert all(muted[ch] for ch in range(8) if ch != 3)

    def test_switching_channels_costs_two_writes_not_sixteen(self, tmp_path):
        # Every mute write is immediately non-volatile. The read-modify-write
        # skips a write that would change nothing, so moving isolation along
        # by one channel is one unmute and one mute.
        backend = a_backend(tmp_path)
        isolator = _proved(backend, SyntheticRig(backend), tmp_path)
        isolator.isolate([0])
        before = backend.device.stats.writes
        isolator.isolate([1])
        assert backend.device.stats.writes - before == 2

    def test_restore_returns_every_output_to_the_snapshot(self, tmp_path):
        backend = a_backend(tmp_path)
        before = [backend.record(ch) for ch in range(8)]
        isolator = _proved(backend, SyntheticRig(backend), tmp_path)
        isolator.isolate([2])
        isolator.restore()
        assert [backend.device.refresh(ch) for ch in range(8)] == before

    def test_a_device_that_ignores_the_mute_write_is_caught(self, tmp_path):
        # The write path's own verification catches a swallowed write; this
        # catches the mute state being wrong for any other reason.
        backend = a_backend(tmp_path)
        isolator = _proved(backend, SyntheticRig(backend), tmp_path)
        backend.read_channel = lambda ch: ChannelConfig(muted=False)
        with pytest.raises(IsolationError, match="did not read back"):
            isolator.isolate([0])

    def test_no_isolation_demands_a_written_basis(self):
        with pytest.raises(IsolationError, match="nothing here can verify"):
            NoIsolation(basis="   ")

    def test_no_isolation_records_its_claim(self):
        claim = "bench: only OUT1 has an RCA in it, 2-8 physically unwired"
        described = NoIsolation(basis=claim).describe()
        assert described["basis"] == claim
        assert described["proved_silent"] is False


class TestIsolationInsideARun:
    def test_the_run_proves_isolation_before_it_measures(self, tmp_path):
        backend = a_backend(tmp_path)
        report = a_run(a_plan(tmp_path), backend, SyntheticRig(backend)).execute()
        stages = [s.stage for s in report.stages]
        assert stages.index(Stage.ISOLATION) < stages.index(Stage.FLOOR)
        assert report.stage_data(Stage.ISOLATION, "proved_silent") is True

    def test_the_tune_does_not_carry_the_isolation_mute(self, tmp_path):
        # The MISC block holds gain, delay and the mute bit together, so a
        # tune write carries whatever mute state the fit happened to see. If
        # that were the isolation state, an accepted tune would leave seven
        # channels muted in the car.
        backend = a_backend(tmp_path)
        report = a_run(a_plan(tmp_path), backend, SyntheticRig(backend)).execute()
        assert report.accepted
        assert not any(backend.read_channel(ch).muted for ch in range(8))

    def test_the_write_manifest_shows_only_the_tune(self, tmp_path):
        # Isolation is restored before the write, so the diff against the
        # baseline is the tune's, not the tune's plus seven mute bits.
        backend = a_backend(tmp_path)
        report = a_run(a_plan(tmp_path), backend, SyntheticRig(backend)).execute()
        assert set(report.stage_data(Stage.WRITE, "changed_blocks")) == {"0"}

    def test_a_rejected_run_leaves_the_mute_states_it_found(self, tmp_path):
        backend = a_backend(tmp_path)
        before = [backend.record(ch) for ch in range(8)]
        report = a_run(a_plan(tmp_path), backend, DeafRig(backend)).execute()
        assert report.verdict.requires_rollback
        assert [backend.device.refresh(ch) for ch in range(8)] == before

    def test_declared_isolation_needs_no_proof_sweep(self, tmp_path):
        backend = a_backend(tmp_path)
        rig = SyntheticRig(backend)
        report = a_run(
            a_plan(tmp_path),
            backend,
            rig,
            isolator=NoIsolation("bench: OUT1 only, nothing else wired"),
        ).execute()
        assert report.accepted
        assert report.stage_data(Stage.ISOLATION, "proved_silent") is False
        assert "silence-proof" not in [tag for _, _, tag in rig.calls]


def _snapshot_of(backend):
    from tuner.dsp import snapshot as snap

    return snap.capture(backend.device, backend.identity)


def _proved(backend, rig, tmp_path, gangs=()):
    """An isolator that has already passed its silence proof."""
    isolator = MuteIsolator(backend, gangs=gangs)
    shot = _snapshot_of(backend)
    isolator.begin(shot)
    backend.device.arm_writes(reason="test", evidence=shot.save(tmp_path / "iso.json"))
    isolator.prove_silence(
        lambda: rig.measure(0, backend.stimulus_limit(0), "silence-proof")
    )
    return isolator


# ---------------------------------------------------------------------------
# Gangs. Outputs 7 and 8 drive two subwoofers in one ported box, so they are
# one source to measure and one correction to write, and mismatching them is a
# mechanical hazard rather than a tonal choice.
# ---------------------------------------------------------------------------

SUBS = Gang(outputs=(6, 7), basis="two subwoofers in one ported box", name="subs")


def a_ganged_plan(tmp_path, **kwargs):
    """A plan that tunes output 1 and the subwoofer gang."""
    return a_plan(
        tmp_path,
        objective=an_objective(outputs=(0, 6)),
        gangs=(SUBS,),
        **kwargs,
    )


def link(backend, channels=(6, 7), group=3):
    """Set linkgroup_num on the fake, the way the vendor app would."""
    fake = backend.device.session.transport.device
    for channel in channels:
        record = bytearray(fake.image.channels[channel])
        record[35 * 8 + 7] = group
        fake.image.channels[channel] = record


class TestGang:
    def test_members_are_sorted_and_deduplicated(self):
        assert Gang((7, 6, 7), basis="shared box").outputs == (6, 7)

    def test_the_leader_is_the_lowest_member(self):
        assert Gang((7, 6), basis="shared box").leader == 6

    def test_a_multi_output_gang_needs_a_basis(self):
        with pytest.raises(PlanError, match="why these outputs move together"):
            Gang((6, 7))

    def test_a_solo_gang_needs_no_basis(self):
        # A single driver is not a claim about anything.
        assert Gang((3,)).is_solo

    def test_an_output_outside_the_device_is_refused(self):
        with pytest.raises(PlanError, match="outside 0..7"):
            Gang((6, 9), basis="shared box")

    def test_the_label_falls_back_to_one_based_outputs(self):
        assert Gang((6, 7), basis="x").label == "7+8"
        assert SUBS.label == "subs"


class TestGangsInThePlan:
    def test_sources_and_outputs_are_different_things(self, tmp_path):
        plan = a_ganged_plan(tmp_path)
        assert plan.sources == (0, 6)
        assert plan.outputs == (0, 6, 7)

    def test_an_undeclared_source_becomes_a_solo_gang(self, tmp_path):
        # Eight separate drivers declare nothing and the run still has one
        # shape to handle rather than two.
        plan = a_plan(tmp_path, objective=an_objective(outputs=(0, 3)))
        assert plan.gang(3).outputs == (3,)
        assert plan.outputs == (0, 3)

    def test_an_output_cannot_be_in_two_gangs(self, tmp_path):
        with pytest.raises(PlanError, match="is in two gangs"):
            a_plan(
                tmp_path,
                objective=an_objective(outputs=(0,)),
                gangs=(Gang((6, 7), basis="a"), Gang((7,), basis="b")),
            )

    def test_weighting_a_follower_is_refused(self, tmp_path):
        # Scoring output 8 while output 7 leads its gang would measure the
        # gang under one name and score it under another.
        with pytest.raises(PlanError, match="a follower in gang"):
            a_plan(tmp_path, objective=an_objective(outputs=(7,)), gangs=(SUBS,))

    def test_weighting_both_members_is_refused(self, tmp_path):
        # A gang is one acoustic source and gets one weight. Weighting both
        # members necessarily weights a follower, which is why there is no
        # separate "counted twice" branch -- it could never execute.
        with pytest.raises(PlanError, match="a follower in gang"):
            a_plan(tmp_path, objective=an_objective(outputs=(6, 7)), gangs=(SUBS,))

    def test_the_gang_reaches_provenance_with_its_basis(self, tmp_path):
        recorded = a_ganged_plan(tmp_path).as_dict()["gangs"]
        assert recorded == [
            {
                "outputs": [6, 7],
                "leader": 6,
                "basis": "two subwoofers in one ported box",
                "name": "subs",
            }
        ]

    def test_changing_a_gang_changes_the_plan_fingerprint(self, tmp_path):
        # Gang membership changes what is measured and what is written, so it
        # is part of what the run was frozen to do.
        a = a_ganged_plan(tmp_path)
        b = a_plan(
            tmp_path,
            objective=an_objective(outputs=(0, 6)),
            gangs=(Gang((6, 7), basis="different reason"),),
        )
        assert a.fingerprint() != b.fingerprint()

    def test_a_gang_sweeps_at_its_most_fragile_members_ceiling(self, tmp_path):
        plan = a_plan(
            tmp_path,
            objective=an_objective(outputs=(6,)),
            gangs=(SUBS,),
            driver_ceilings={
                6: DriverCeiling(-6.0, "12in sub, sealed side"),
                7: DriverCeiling(-14.0, "the quieter of the pair"),
            },
        )
        # Swept as one, so the stimulus reaches both. The minimum is the only
        # safe answer.
        assert plan.ceiling_for_source(6) == (-14.0, True)

    def test_one_uncharacterized_member_makes_the_gang_uncharacterized(self, tmp_path):
        plan = a_plan(
            tmp_path,
            objective=an_objective(outputs=(6,)),
            gangs=(SUBS,),
            driver_ceilings={6: DriverCeiling(-6.0, "known")},
        )
        ceiling, characterized = plan.ceiling_for_source(6)
        assert characterized is False
        assert ceiling == DEFAULT_CEILING_DBFS


class TestGangsInTheIsolator:
    def test_a_gang_goes_audible_together(self, tmp_path):
        backend = a_backend(tmp_path)
        isolator = _proved(backend, SyntheticRig(backend), tmp_path, gangs=((6, 7),))
        isolator.isolate((6, 7))
        muted = [backend.read_channel(ch).muted for ch in range(8)]
        assert muted[6] is False and muted[7] is False
        assert all(muted[ch] for ch in range(6))

    def test_a_declared_gang_lifts_the_linked_channel_refusal(self, tmp_path):
        backend = a_backend(tmp_path)
        link(backend)
        isolator = MuteIsolator(backend, gangs=((6, 7),))
        isolator.begin(_snapshot_of(backend))  # no raise

    def test_a_link_group_with_no_gang_is_still_refused(self, tmp_path):
        backend = a_backend(tmp_path)
        link(backend)
        with pytest.raises(IsolationError, match="in a link group"):
            MuteIsolator(backend).begin(_snapshot_of(backend))

    def test_a_gang_covering_a_link_group_only_in_part_is_refused(self, tmp_path):
        # Measured together, written apart -- the worst of both.
        backend = a_backend(tmp_path)
        link(backend, channels=(5, 6, 7))
        with pytest.raises(IsolationError, match=r"covers part of a device link group"):
            MuteIsolator(backend, gangs=((6, 7),)).begin(_snapshot_of(backend))

    def test_a_declared_gang_the_device_calls_unlinked_warns_but_proceeds(
        self, tmp_path
    ):
        # The observed state of outputs 7 and 8 in 14 of the 40 .DDP backups.
        # The run is safer than the device here -- it writes both members
        # identically because the gang says so -- but the device is in a state
        # where the vendor app could move one alone, and that is worth saying.
        backend = a_backend(tmp_path)
        isolator = MuteIsolator(backend, gangs=((6, 7),))
        isolator.begin(_snapshot_of(backend))
        assert any("linkgroup_num as 0" in w for w in isolator.warnings)
        assert any("Re-link them" in w for w in isolator.warnings)

    def test_a_linked_gang_does_not_warn(self, tmp_path):
        backend = a_backend(tmp_path)
        link(backend)
        isolator = MuteIsolator(backend, gangs=((6, 7),))
        isolator.begin(_snapshot_of(backend))
        assert isolator.warnings == ()


class TestGangsInARun:
    def test_a_ganged_run_is_accepted_and_both_subs_get_the_same_tune(self, tmp_path):
        backend = a_backend(tmp_path)
        report = a_run(
            a_ganged_plan(tmp_path),
            backend,
            SyntheticRig(backend),
            isolator=MuteIsolator(backend, gangs=((6, 7),)),
        ).execute()
        assert report.accepted, report.summary()
        assert backend.tuning_digest(6) == backend.tuning_digest(7)

    def test_the_gang_is_swept_once_not_twice(self, tmp_path):
        backend = a_backend(tmp_path)
        rig = SyntheticRig(backend)
        a_run(
            a_ganged_plan(tmp_path),
            backend,
            rig,
            isolator=MuteIsolator(backend, gangs=((6, 7),)),
        ).execute()
        swept = {output for output, _, tag in rig.calls if tag == "baseline-x"}
        del swept
        # Two sources, three floor repeats, one verify: never output 7 alone.
        assert all(output in (0, 6) for output, _, _ in rig.calls)

    def test_a_device_that_writes_one_member_only_is_caught(self, tmp_path):
        # The failure the gang exists to prevent. Simulated by letting the
        # write land and then quietly reverting output 8 -- which is what a
        # refused frame, a partial write or an off-by-one channel id would
        # look like from here.
        backend = a_backend(tmp_path)
        before = backend.record(7)
        real_write = backend.write_channel

        def drop_the_second_sub(output, config):
            real_write(output, config)
            if output == 7:
                fake = backend.device.session.transport.device
                fake.image.channels[7] = bytearray(before)
                backend.device.refresh(7)

        backend.write_channel = drop_the_second_sub
        report = a_run(
            a_ganged_plan(tmp_path),
            backend,
            SyntheticRig(backend),
            isolator=MuteIsolator(backend, gangs=((6, 7),)),
        ).execute()
        assert "does not hold one tune" in report.error
        assert report.rollback.device_matches

    def test_a_gang_mismatched_before_the_run_stops_it(self, tmp_path):
        # Two subwoofers already holding different tunes may already be doing
        # damage. Levelling them silently would change something nobody asked
        # us to change, and a rollback would put the mismatch back anyway, so
        # the honest options are all the operator's.
        backend = a_backend(tmp_path)
        backend.device.arm_writes(
            reason="setup", evidence=_snapshot_of(backend).save(tmp_path / "s.json")
        )
        backend.write_channel(7, replace(backend.read_channel(7), gain_dbfs=-13.0))
        backend.device.disarm()

        report = a_run(
            a_ganged_plan(tmp_path),
            backend,
            SyntheticRig(backend),
            isolator=MuteIsolator(backend, gangs=((6, 7),)),
        ).execute()
        assert "before any write" in report.error
        assert "two subwoofers in one ported box" in report.error

    def test_nothing_was_swept_before_that_refusal(self, tmp_path):
        backend = a_backend(tmp_path)
        backend.device.arm_writes(
            reason="setup", evidence=_snapshot_of(backend).save(tmp_path / "s.json")
        )
        backend.write_channel(7, replace(backend.read_channel(7), gain_dbfs=-13.0))
        backend.device.disarm()

        rig = SyntheticRig(backend)
        a_run(
            a_ganged_plan(tmp_path),
            backend,
            rig,
            isolator=MuteIsolator(backend, gangs=((6, 7),)),
        ).execute()
        assert rig.calls == []

    def test_the_budget_charges_a_gang_per_output(self, tmp_path):
        # Delay RAM is a per-channel cost. A two-driver gang spends it twice,
        # and a budget that counted sources would under-report by half.
        plan = a_ganged_plan(tmp_path)
        assert len(plan.outputs) == 3
        assert len(plan.sources) == 2

    def test_the_isolation_warning_reaches_the_run_report(self, tmp_path):
        backend = a_backend(tmp_path)
        report = a_run(
            a_ganged_plan(tmp_path),
            backend,
            SyntheticRig(backend),
            isolator=MuteIsolator(backend, gangs=((6, 7),)),
        ).execute()
        warnings = [
            s.detail
            for s in report.stages
            if s.stage is Stage.ISOLATION and s.data.get("warning")
        ]
        assert any("linkgroup_num as 0" in w for w in warnings)

    def test_a_run_over_linked_outputs_transmits(self, tmp_path):
        # txpolicy refuses writes to linked channels by default, and should:
        # writing one half leaves the device disagreeing with the model. A
        # declared gang is the caller saying it writes every member, which is
        # the one case the refusal is wrong for.
        backend = a_backend(tmp_path)
        link(backend)
        report = a_run(
            a_ganged_plan(tmp_path),
            backend,
            SyntheticRig(backend),
            isolator=MuteIsolator(backend, gangs=((6, 7),)),
        ).execute()
        assert report.accepted, report.summary()
        assert backend.tuning_digest(6) == backend.tuning_digest(7)


class TestTuningDigest:
    def test_a_flat_band_layout_does_not_make_two_channels_differ(self, tmp_path):
        # The vendor app seeds each channel's unused slots with its own
        # default frequencies. Those bands are at 0 dB and inaudible, so two
        # untouched channels hold the same tune -- and an earlier digest that
        # hashed them verbatim would have failed every ganged run.
        backend = a_backend(tmp_path)
        assert backend.tuning_digest(6) == backend.tuning_digest(7)

    def test_a_real_difference_still_shows(self, tmp_path):
        backend = a_backend(tmp_path)
        backend.device.arm_writes(
            reason="test", evidence=_snapshot_of(backend).save(tmp_path / "d0.json")
        )
        backend.write_channel(
            0,
            replace(
                backend.read_channel(0),
                peq=(Biquad(1_000.0, -3.0, 2.0, FilterType.PEAKING),),
            ),
        )
        assert backend.tuning_digest(0) != backend.tuning_digest(1)

    def test_the_same_tune_on_two_channels_matches(self, tmp_path):
        backend = a_backend(tmp_path)
        backend.device.arm_writes(
            reason="test", evidence=_snapshot_of(backend).save(tmp_path / "d.json")
        )
        config = replace(
            backend.read_channel(0),
            gain_dbfs=-12.0,
            peq=(Biquad(1_000.0, -3.0, 2.0, FilterType.PEAKING),),
        )
        backend.write_channel(0, config)
        backend.write_channel(1, config)
        assert backend.tuning_digest(0) == backend.tuning_digest(1)

    def test_mute_is_not_part_of_the_tune(self, tmp_path):
        # Isolation mutes and unmutes constantly; a digest that moved with it
        # would make the gang check fire on every sweep.
        backend = a_backend(tmp_path)
        backend.device.arm_writes(
            reason="test", evidence=_snapshot_of(backend).save(tmp_path / "d.json")
        )
        before = backend.tuning_digest(0)
        backend.set_muted(0, True)
        assert backend.tuning_digest(0) == before

    def test_gain_is_part_of_the_tune(self, tmp_path):
        backend = a_backend(tmp_path)
        backend.device.arm_writes(
            reason="test", evidence=_snapshot_of(backend).save(tmp_path / "d.json")
        )
        before = backend.tuning_digest(0)
        backend.write_channel(0, replace(backend.read_channel(0), gain_dbfs=-13.0))
        assert backend.tuning_digest(0) != before


def _preload_band(backend, output: int, *bands) -> None:
    """Put EQ on a channel the way the operator's app would have.

    Bypasses ``write_channel`` deliberately: that path needs arming, and what
    these tests need is a device that *arrives* already equalised, which is
    the state every channel of the development car is in.
    """
    from tuner.dsp.dsp408_spp import _band_to_eq
    from tuner.dsp.protocol import EqBand

    fake = backend.device.session.transport.device
    record = bytearray(fake.image.channels[output])
    for i, band in enumerate(bands):
        stored = EqBand.decode(bytes(record[i * 8 : (i + 1) * 8]))
        record[i * 8 : (i + 1) * 8] = _band_to_eq(band, stored).encode()
    fake.image.channels[output] = record
    backend.device.refresh(output)


class TestAChannelThatAlreadyHasEq:
    """A run must land in the same place whether or not the channel starts flat.

    **The regression test for the worst defect this project has found.** The
    run measures a baseline with the channel's existing EQ loaded, then writes
    EXCLUSIVE, which *replaces* that EQ. Fitting against the measurement
    therefore solved ``raw + existing + fitted = target`` while the device
    delivers ``raw + fitted`` -- the existing EQ counted twice.

    Found 2026-08-11 offline, before the closed loop ever ran on hardware, and
    fixed by ``TuneRun._without_existing_eq``. Every channel on the development
    car has EQ loaded, so the first real tune would have met it.

    Both runs here have noise disabled, so any difference is the algorithm.
    """

    #: +8 dB at 1200 Hz, wide enough for the fitter to have to deal with it.
    #: A **boost** deliberately: the fitter's ``max_boost_db`` of 3.0 means a
    #: pre-existing *cut* is one it may not try to correct, which is what made
    #: the first attempt at this experiment look like a refutation.
    PRELOAD = Biquad(freq_hz=1200.0, gain_dbfs=8.0, q=2.0, kind=FilterType.PEAKING)

    def _final_response(self, tmp_path, preload):
        backend = a_backend(tmp_path)
        if preload is not None:
            # Straight onto the fake's records rather than through
            # write_channel, which would need arming. The point is to reach
            # the run with the device already equalised, the way the car is.
            _preload_band(backend, 0, preload)
        rig = SyntheticRig(backend, noise_db=0.0)
        plan = a_plan(tmp_path)
        report = a_run(plan, backend, rig).execute()

        from tuner.safety.limits import ChannelLimit

        axis = plan.objective.freqs_hz
        limit = ChannelLimit(DEFAULT_CEILING_DBFS, characterized=False)
        final = rig.measure(0, limit, "final")[0].magnitude_dbfs(axis)
        band = plan.objective.band_hz
        mask = (axis >= band[0]) & (axis <= band[1])
        return report, final - np.mean(final[mask]), mask

    def test_a_preloaded_channel_scores_the_same_as_a_flat_one(self, tmp_path):
        """The property that matters: the tune ends up as good either way.

        Not "the two responses are identical". Differential evolution is a
        stochastic global search, and the two runs see inputs that differ by
        the synthetic rig's FFT round-trip, so they legitimately land on
        different band sets of equivalent quality. Scoring them is what
        "lands in the same place" means for a tuner.

        Before the fix: 1.202 vs 1.461 rms and the responses 5.9 dB apart.
        """
        flat_report, flat_start, mask = self._final_response(tmp_path, None)
        pre_report, preloaded, _ = self._final_response(tmp_path, self.PRELOAD)

        # **Neither of the first two assertions guards anything, and that is
        # the point of writing them down.** Measured with the fix reverted:
        # the score gap was 0.233 dB and the rms spread 0.735 dB, both inside
        # any threshold loose enough to tolerate two stochastic fits -- while
        # the response was 6.3 dB out.
        #
        # An rms objective over 200 log-spaced points barely registers a
        # narrow error, which is exactly why the improvement invariant
        # reported `accepted` on both the broken and the fixed run. Peak
        # deviation is what separates them: 1.06 dB fixed, 6.30 dB broken.
        gap = abs(flat_report.verdict.result_score - pre_report.verdict.result_score)
        assert gap < 0.3, f"scores differ by {gap:.3f} dB"

        spread = float(np.sqrt(np.mean(((preloaded - flat_start)[mask]) ** 2)))
        assert spread < 0.75, f"responses differ by {spread:.3f} dB rms"

        worst = float(np.abs((preloaded - flat_start)[mask]).max())
        assert worst < 2.5, f"responses differ by {worst:.2f} dB at worst"

    def test_no_band_is_spent_cancelling_the_preloaded_one(self, tmp_path):
        """The defect's actual signature, which is sharper than any tolerance.

        The bug made the fit cancel EQ that the write was about to delete. On
        this speaker nothing wants correcting near 1200 Hz -- the modelled
        problems are at 300 and 3000 -- so a big band there is the fingerprint
        and nothing else.

        Before the fix: **-6.2 dB at 1132 Hz**, almost exactly undoing the
        +8 dB preload. After: nothing above half a dB anywhere near it.
        """
        backend = a_backend(tmp_path)
        _preload_band(backend, 0, self.PRELOAD)
        a_run(a_plan(tmp_path), backend, SyntheticRig(backend, noise_db=0.0)).execute()

        near = [
            b
            for b in backend.read_channel(0).peq
            if abs(np.log2(b.freq_hz / self.PRELOAD.freq_hz)) < 0.5
        ]
        worst = max((abs(b.gain_dbfs) for b in near), default=0.0)
        assert worst < 3.0, (
            f"a {worst:.1f} dB band sits within half an octave of the "
            f"preloaded one; the fit is still cancelling EQ the write removes"
        )

    def test_the_existing_eq_is_reported_as_removed(self, tmp_path):
        report, _, _ = self._final_response(tmp_path, self.PRELOAD)
        detail = report.stage(Stage.FIT).data["per_output"][0]
        assert detail["existing_bands_removed"] == 1
        # An 8 dB band should show up as roughly 8 dB of removed EQ.
        assert detail["existing_eq_peak_db"] == pytest.approx(8.0, abs=0.5)

    def test_a_flat_channel_reports_nothing_removed(self, tmp_path):
        report, _, _ = self._final_response(tmp_path, None)
        detail = report.stage(Stage.FIT).data["per_output"][0]
        assert detail["existing_bands_removed"] == 0
        assert detail["existing_eq_peak_db"] == 0.0

    def test_more_bands_than_the_device_supports_is_refused(self, tmp_path):
        # The subtraction assumes every band read back is executing. Beyond
        # the supported ceiling that is exactly the open question about EQ
        # slots 11-30, so the run refuses rather than guessing -- and refuses
        # at the fit, before anything is measured against a model nobody can
        # justify.
        backend = a_backend(tmp_path)
        too_many = tuple(
            Biquad(freq_hz=200.0 * (i + 1), gain_dbfs=-2.0, q=1.0)
            for i in range(backend.limits.max_peq_per_channel + 1)
        )
        _preload_band(backend, 0, *too_many)
        assert len(backend.read_channel(0).peq) > backend.limits.max_peq_per_channel

        report = a_run(a_plan(tmp_path), backend, SyntheticRig(backend)).execute()
        assert report.verdict is None or report.verdict.outcome is Outcome.ABORTED


# ---------------------------------------------------------------------------
# The setup token. Temperature was the only environmental term this project
# gated on, and it is the weakest one -- microphone position, seat position,
# doors, HVAC and occupancy all move the response further and none is visible
# to code. The token is the operator's verbatim claim that none of them
# changed, and these tests pin *when* its absence is discovered.
# ---------------------------------------------------------------------------


class TestTheSetupToken:
    def _acoustic_rig(self, monkeypatch, **session_kwargs):
        from tuner.measure.capture import CaptureConfig, SessionInfo
        from tuner.orchestrate import rig as rig_module

        monkeypatch.setattr(
            rig_module, "capture_sweep", lambda c, s, now=None: {1: _flat_measurement()}
        )
        return rig_module.AcousticMeasurer(
            config=CaptureConfig(sample_rate_hz=SAMPLE_RATE_HZ, input_channels=(1,)),
            session=SessionInfo(gains_db=(30.0,), **session_kwargs),
            positions=("driver",),
            linearity=_linear_result(),
        )

    def test_an_acoustic_rig_without_a_token_is_refused_at_construction(
        self, monkeypatch
    ):
        # Before any sweep, before ARM, before the device is touched. An
        # acoustic session with no token can never produce a verdict, so the
        # earliest possible refusal is the right one.
        from tuner.orchestrate.rig import RigError

        with pytest.raises(RigError, match="setup token"):
            self._acoustic_rig(monkeypatch)

    def test_an_electrical_rig_needs_none(self, monkeypatch):
        from tuner.measure.result import Coupling

        assert self._acoustic_rig(monkeypatch, coupling=Coupling.ELECTRICAL)

    def test_a_declared_token_reaches_provenance(self, monkeypatch):
        from tuner.measure.capture import CaptureConfig, SessionInfo
        from tuner.orchestrate import rig as rig_module
        from tuner.safety.limits import ChannelLimit

        seen: list = []
        monkeypatch.setattr(
            rig_module,
            "capture_sweep",
            lambda c, s, now=None: (seen.append(s), {1: _flat_measurement()})[1],
        )
        rig_module.AcousticMeasurer(
            config=CaptureConfig(sample_rate_hz=SAMPLE_RATE_HZ, input_channels=(1,)),
            session=SessionInfo(
                gains_db=(30.0,), setup_token="driver seat, doors shut"
            ),
            positions=("driver",),
            linearity=_linear_result(),
        ).measure(0, ChannelLimit(), "baseline")
        assert seen[0].setup_token == "driver seat, doors shut"

    def test_a_run_that_can_never_reach_a_verdict_stops_before_the_write(
        self, tmp_path
    ):
        """The lesson from 2026-08-12, generalised past temperature.

        That run armed, measured, fitted, wrote eleven blocks, and only then
        found at VERIFY that no thermometer reading had been supplied -- so
        the verdict was indeterminate and the device had to be rolled back.
        Nothing was wrong with the tune. The run could have known before it
        changed anything, because the defect was structural: a provenance
        that is not comparable *to itself* can never be compared to a
        verification sweep either.
        """
        backend = a_backend(tmp_path)
        before = [backend.record(ch) for ch in range(8)]

        report = a_run(
            a_plan(tmp_path), backend, SyntheticRig(backend, setup_token=None)
        ).execute()

        assert not report.accepted
        # The sharp assertion. Not "it failed" -- *where* it failed. A run
        # that reaches WRITE and then rolls back passes any weaker check.
        stages = [s.stage for s in report.stages]
        assert Stage.ISOLATION in stages  # it did get as far as measuring
        assert Stage.FIT not in stages
        assert Stage.WRITE not in stages
        assert [backend.device.refresh(ch) for ch in range(8)] == before

    def test_the_refusal_names_the_missing_term(self, tmp_path):
        backend = a_backend(tmp_path)
        report = a_run(
            a_plan(tmp_path), backend, SyntheticRig(backend, setup_token=None)
        ).execute()
        assert "setup token" in report.error
        assert "Nothing has been written" in report.error

    def test_a_run_with_a_token_still_accepts(self, tmp_path):
        # Vacuity check: the run above must fail *because of the token*, not
        # because this harness cannot pass at all.
        backend = a_backend(tmp_path)
        report = a_run(a_plan(tmp_path), backend, SyntheticRig(backend)).execute()
        assert report.accepted


class TestTheFloorBracketsTheRun:
    """The floor's repeats must span the interval the verdict compares.

    Back-to-back repeats measure thirty seconds of noise, and both halves of
    the improvement invariant compare measurements minutes apart. The fix
    costs nothing: one of the repeats moves to the latest point at which the
    device still holds the baseline, which is after the fit and before the
    write.
    """

    def test_the_last_repeat_happens_after_the_fit(self, tmp_path):
        # The defect's signature, and the only assertion here that catches
        # it. `span_s > 0` would pass with every repeat still clustered at
        # the start; where the last one sits is the whole change.
        backend = a_backend(tmp_path)
        report = a_run(a_plan(tmp_path), backend, SyntheticRig(backend)).execute()
        assert report.accepted
        stages = [s.stage for s in report.stages]
        assert stages.index(Stage.BASELINE) < stages.index(Stage.FIT)
        assert stages.index(Stage.FIT) < stages.index(Stage.FLOOR)
        assert stages.index(Stage.FLOOR) < stages.index(Stage.WRITE)

    def test_it_costs_no_extra_sweeps(self, tmp_path):
        # A repeat was moved, not added. If this ever regresses to adding
        # one, an eight-source run pays a whole extra measurement round.
        backend = a_backend(tmp_path)
        rig = SyntheticRig(backend)
        plan = a_plan(tmp_path)
        a_run(plan, backend, rig).execute()
        floor_tags = {tag for _, _, tag in rig.calls if tag.startswith("floor-")}
        assert floor_tags == {f"floor-{i}" for i in range(plan.floor_repeats)}

    def test_the_floor_records_the_window_it_measured(self, tmp_path):
        backend = a_backend(tmp_path)
        report = a_run(a_plan(tmp_path), backend, SyntheticRig(backend)).execute()
        assert report.floor is not None
        assert report.floor.span_s > 0.0
        assert report.as_dict()["floor"]["span_s"] == report.floor.span_s
        assert report.stage_data(Stage.FLOOR, "span_s") is not None

    def test_a_floor_too_short_for_its_verdict_is_reported(self, tmp_path):
        # Recorded, not enforced. Refusing would need a model of how the
        # rig's noise grows with time, and no such measurement exists --
        # inventing one is what this project's rules forbid.
        backend = a_backend(tmp_path)
        run = a_run(a_plan(tmp_path), backend, SyntheticRig(backend))
        original = run._close_floor

        def barely_spanning():
            return replace(original(), span_s=0.0)

        run._close_floor = barely_spanning
        report = run.execute()
        assert report.accepted  # a warning, not a verdict change
        assert (
            report.stage_data(Stage.VERIFY, "warning") == "floor_shorter_than_interval"
        )
        assert report.stage_data(Stage.VERIFY, "verdict_interval_s") is not None

    def test_a_floor_that_spans_the_run_says_nothing(self, tmp_path):
        # Vacuity check for the test above: the warning must not always fire.
        backend = a_backend(tmp_path)
        report = a_run(a_plan(tmp_path), backend, SyntheticRig(backend)).execute()
        assert report.stage_data(Stage.VERIFY, "warning") is None
