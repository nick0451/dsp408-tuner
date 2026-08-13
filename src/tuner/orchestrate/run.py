"""The tuning run: measure, fit, write once, re-measure, accept or roll back.

Stage order is not negotiable, and each ordering constraint exists because
getting it wrong produces a run that *looks* correct:

``ARM`` before anything
    Two restore paths on disk and in a preset slot, both verified, before a
    single byte is written. A run that arms after measuring has a window in
    which a crash leaves no way back.

``FLOOR`` and ``BASELINE`` are one measurement campaign, and it brackets the run
    The floor is the spread of *the objective*, so it is measured by scoring
    the same full measurement set several times -- not by repeating one
    convenient sweep and hoping the statistics carry. The last of the opening
    repeats becomes the baseline, because it is the one closest in time to the
    writes.

    **The floor's final repeat is taken after the fit, not with the others.**
    Repeats clustered back to back measure thirty seconds of noise and are
    blind to drift over a run lasting minutes -- and both halves of the
    invariant compare measurements minutes apart. Moving one repeat to the
    latest point at which the device still holds the baseline costs no extra
    sweeps and makes ``span_s`` cover the run. See ``_close_floor``.

``FIT`` touches no hardware
    Every write goes straight to non-volatile storage, so the optimizer
    evaluates candidates against its model and only the chosen configuration
    reaches the device. This is also why ``WRITE`` is a single stage rather
    than a loop.

``VERIFY`` re-checks the freeze before it scores
    The plan and the objective are fingerprinted at construction and re-hashed
    here. An objective that moved does not degrade the verdict, it voids it.

``SETTLE`` proves a rollback acoustically
    Byte-equality proves the settings went back. It does not prove the system
    did -- a rollback that restores the bytes while the microphone has moved,
    a door is open or the amplifier has warmed is not a rollback to the
    baseline state, and reporting one as clean is how a run silently leaves
    the car changed. So the restored system is re-measured and its score must
    land within the session floor of the baseline.

Every stage that sweeps reads the channel's gain and EQ back from the device
first and subtracts them from the ceiling. That is hard safety rule 6, and the
closed loop is exactly the sequence it exists for: the optimizer writes a
boost, then immediately sweeps the channel it boosted, with no human between.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Protocol

import numpy as np

from ..dsp import snapshot as snap
from ..dsp.backend import ChannelConfig
from ..dsp.dsp408_spp import Dsp408Spp, PeqPolicy
from ..dsp.protocol import PRESET_NAME_LEN
from ..measure.result import Measurement
from ..optimize import biquad, budget, delay
from ..optimize.verify import (
    Outcome,
    RepeatabilityFloor,
    Verdict,
    measure_repeatability,
    verify,
)
from ..safety.limits import ChannelLimit
from .isolate import Isolator
from .objective import ObjectiveError
from .plan import TunePlan


class OrchestrationError(RuntimeError):
    """The run cannot continue. Carries the partial report where one exists."""

    def __init__(self, message: str, report: RunReport | None = None) -> None:
        super().__init__(message)
        self.report = report


class RollbackFailed(OrchestrationError):
    """Both restore paths were tried and the system did not come back.

    The most serious outcome this module can produce: the run has changed the
    car and cannot undo it. Raised loudly, never swallowed, and the message
    names the remaining manual routes -- a ``.DDP`` load through the vendor
    app, or recalling the operator's own base preset.
    """


class Stage(Enum):
    """Stages in order. A run records one :class:`StageRecord` per stage."""

    ARM = "arm"
    ISOLATION = "isolation"
    FLOOR = "floor"
    BASELINE = "baseline"
    FIT = "fit"
    WRITE = "write"
    VERIFY = "verify"
    SETTLE = "settle"


@dataclass(frozen=True)
class StageRecord:
    """What one stage did, whether it succeeded, and the evidence."""

    stage: Stage
    ok: bool
    detail: str
    data: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage.value,
            "ok": self.ok,
            "detail": self.detail,
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class RunReport:
    """Everything a run did, in a form that can be written to disk verbatim.

    This *is* the provenance record the improvement invariant requires. The
    plan fingerprint, the objective fingerprint, the floor and the number of
    repeats that established it, both scores and the verdict's own reason are
    all here, so a later reader can tell whether the comparison was legitimate
    without re-deriving it.
    """

    plan_fingerprint: str
    objective_fingerprint: str
    stages: tuple[StageRecord, ...]
    verdict: Verdict | None = None
    floor: RepeatabilityFloor | None = None
    rollback: snap.PresetRestoreReport | snap.RestoreReport | None = None
    rollback_verified_acoustically: bool | None = None
    error: str | None = None
    plan: Mapping[str, object] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.verdict is not None and self.verdict.outcome is Outcome.ACCEPTED

    @property
    def outcome(self) -> Outcome | None:
        return self.verdict.outcome if self.verdict else None

    def stage(self, stage: Stage) -> StageRecord | None:
        for record in self.stages:
            if record.stage is stage:
                return record
        return None

    def stage_data(
        self, stage: Stage | str, key: str, default: object = None
    ) -> object:
        """One datum from a stage, taking the **last** record that carries it.

        Last rather than first: a stage may record more than once -- FLOOR
        adds a second entry when the spread comes out at exactly zero, SETTLE
        when a restore path fails and another is tried -- and in both cases
        the later record is the one describing what actually happened.
        """
        wanted = Stage(stage) if isinstance(stage, str) else stage
        found = default
        for record in self.stages:
            if record.stage is wanted and key in record.data:
                found = record.data[key]
        return found

    def as_dict(self) -> dict[str, object]:
        return {
            "plan_fingerprint": self.plan_fingerprint,
            "objective_fingerprint": self.objective_fingerprint,
            "plan": dict(self.plan),
            "stages": [s.as_dict() for s in self.stages],
            "verdict": (
                None
                if self.verdict is None
                else {
                    "outcome": self.verdict.outcome.value,
                    "baseline_score": self.verdict.baseline_score,
                    "result_score": self.verdict.result_score,
                    "delta": self.verdict.delta,
                    "floor": self.verdict.floor,
                    "reason": self.verdict.reason,
                }
            ),
            "floor": (
                None
                if self.floor is None
                else {
                    "value": self.floor.value,
                    "n_repeats": self.floor.n_repeats,
                    "session_id": self.floor.session_id,
                    "span_s": self.floor.span_s,
                }
            ),
            "rollback": None if self.rollback is None else self.rollback.summary(),
            "rollback_verified_acoustically": self.rollback_verified_acoustically,
            "error": self.error,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)

    def summary(self) -> str:
        lines = [
            f"outcome: {self.outcome.value if self.outcome else 'aborted'}",
            f"plan {self.plan_fingerprint[:12]} "
            f"objective {self.objective_fingerprint[:12]}",
        ]
        for record in self.stages:
            lines.append(
                f"  [{'ok' if record.ok else 'XX'}] {record.stage.value}: "
                f"{record.detail}"
            )
        if self.verdict is not None:
            lines.append(f"  {self.verdict.reason}")
        if self.rollback is not None:
            lines.append(f"  rollback: {self.rollback.summary()}")
            lines.append(
                f"  rollback re-measured: {self.rollback_verified_acoustically}"
            )
        if self.error:
            lines.append(f"  error: {self.error}")
        return "\n".join(lines)


class Measurer(Protocol):
    """Whatever turns "sweep this output" into measurements.

    One implementation wraps :func:`tuner.measure.capture.capture_sweep` and
    needs a rig; tests substitute a synthetic one. Keeping it a protocol is
    what lets the entire run -- including both rollback paths and every abort
    branch -- be rehearsed with no audio hardware.

    Implementations **must** honour ``limit``: it already has the device's own
    gain and EQ boost subtracted, and the run has no other way to enforce that.
    """

    def measure(
        self, output: int, limit: ChannelLimit, tag: str
    ) -> Sequence[Measurement]:
        """One measurement per listening position, in the objective's order.

        ``tag`` names the campaign (``"floor-0"``, ``"baseline"``,
        ``"verify"``, ``"rollback"``) and exists so a rig that prompts an
        operator to move a microphone can say what the sweep is for.
        """
        ...


def power_average_db(
    curves_db: Sequence[np.ndarray], weights: Sequence[float]
) -> np.ndarray:
    """Combine per-position magnitude curves into one, in dB.

    Averaging is done on **power**, not on dB. Sound pressure from the same
    source at different seats is essentially uncorrelated in phase, so energy
    is the quantity that adds; a mean of dB values is a geometric mean of
    power, which systematically understates peaks and over-rewards a response
    that is bad in one place and good in another. The spatial average is
    supposed to represent the compromise, not flatter it.
    """
    stack = np.asarray(curves_db, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)[:, None]
    power = np.sum(w * 10.0 ** (stack / 10.0), axis=0) / np.sum(w)
    return 10.0 * np.log10(power)


@dataclass
class TuneRun:
    """One closed tuning loop against one device.

    Takes :class:`~tuner.dsp.dsp408_spp.Dsp408Spp` rather than the abstract
    backend, deliberately: rollback needs whole 296-byte records and preset
    slots, neither of which ``DspBackend`` exposes and neither of which the
    simulator has. That costs nothing in testability, because ``Dsp408Spp``
    driven through :class:`~tuner.dsp.fake_device.FakeDsp408` exercises the
    real framing, session, journal, snapshot and preset paths offline.
    """

    plan: TunePlan
    backend: Dsp408Spp
    measurer: Measurer
    isolator: Isolator

    _stages: list[StageRecord] = field(default_factory=list, init=False)
    _snapshot: snap.DeviceSnapshot | None = field(default=None, init=False)
    #: Floor scores accumulate across two stages -- see _baseline and
    #: _close_floor -- so they outlive either one.
    _floor_scores: list[float] = field(default_factory=list, init=False)
    _floor_started_s: float = field(default=0.0, init=False)
    _baseline_at_s: float = field(default=0.0, init=False)
    _frozen_plan: str = field(default="", init=False)
    _frozen_objective: str = field(default="", init=False)

    def __post_init__(self) -> None:
        # A tuning run must own every band on the channels it tunes.
        #
        # Under LEADING, the fit writes bands 0..n-1 and leaves the rest as it
        # found them. Fit eight bands today and five tomorrow, and bands six
        # to eight of yesterday's tune are still running -- filters the
        # optimizer did not model, on a channel whose predicted response it is
        # about to compare a measurement against. Every layer would look
        # correct: the writes succeed, the readbacks match, the fit is good,
        # and the verdict is about a system that was never configured as
        # predicted. EXCLUSIVE flattens the remainder, which is the only
        # policy under which "the device now holds the fitted chain" is true.
        if self.backend.peq_policy is not PeqPolicy.EXCLUSIVE:
            raise OrchestrationError(
                f"a tuning run needs PeqPolicy.EXCLUSIVE, not "
                f"{self.backend.peq_policy.name}: under any other policy a fit "
                f"with fewer bands than the previous one leaves the surplus "
                f"bands running, and the improvement invariant would then "
                f"compare a measurement against a model missing them"
            )

        # Fingerprints are taken here, at construction, and never recomputed
        # into these slots. That is what makes the freeze a freeze: an
        # objective edited after this point changes its hash, and _verify
        # refuses to produce a verdict rather than reporting a comparison
        # between a baseline scored one way and a result scored another.
        self._frozen_plan = self.plan.fingerprint()
        self._frozen_objective = self.plan.objective.fingerprint()

    # -- public ------------------------------------------------------------

    def execute(self) -> RunReport:
        """Run every stage. Returns a report; raises only if rollback fails.

        A failure after ``ARM`` triggers a rollback and is reported, not
        raised -- an aborted run that restored the car is an ordinary outcome
        and the caller wants the evidence, not a traceback. The one exception
        is :class:`RollbackFailed`, which is not an outcome but a situation.
        """
        try:
            self._arm()
            self._prove_isolation()
            baseline = self._baseline()
            configs = self._fit(baseline)
            # The floor's last repeat is taken here, as late as the baseline
            # state survives -- after the fit, which writes nothing, and
            # before the write, which is the moment the state stops being the
            # baseline. That is what makes the repeats span the run rather
            # than the thirty seconds at the start of it.
            floor = self._close_floor()
            self._write(configs)
            verdict = self._verify(baseline, floor)
        except RollbackFailed:
            raise
        except Exception as exc:  # noqa: BLE001 -- reported, then rolled back
            return self._abort(exc)

        return self._settle(verdict, floor, baseline)

    # -- stages ------------------------------------------------------------

    def _arm(self) -> None:
        """Two restore paths, both verified, before anything is written."""
        identity = self.backend.identity
        device = self.backend.device

        # The slot the device is currently running from is the operator's
        # manual fallback: recalling it is how a person puts the car back when
        # everything we built has failed. Storing our baseline over it removes
        # that, and the run would still look correct because it has two other
        # restore paths. One digit's typo in the plan is all it takes.
        if self.plan.scratch_slot == identity.current_preset:
            raise OrchestrationError(
                f"slot {self.plan.scratch_slot} is the preset the device is "
                f"currently running from. Storing the baseline there would "
                f"overwrite the operator's own fallback -- the one thing that "
                f"restores this car without any of our code being correct. "
                f"Choose a different scratch slot."
            )

        baseline_snapshot = snap.capture(
            device,
            identity,
            transport_name=type(device.session.transport).__name__,
            notes={
                "session_id": self.plan.session_id,
                "plan_fingerprint": self.plan.fingerprint(),
                "objective_fingerprint": self.plan.objective.fingerprint(),
                "scratch_slot": str(self.plan.scratch_slot),
                "scratch_slot_confirmed_by": self.plan.scratch_slot_confirmed_by,
                "scratch_slot_holds": (
                    self.plan.scratch_slot_holds or "(believed empty)"
                ),
            },
        )
        evidence = baseline_snapshot.save(self.plan.snapshot_path)
        evidence.verify()
        self._snapshot = baseline_snapshot

        # Store to the slot before arming block writes: the preset store is
        # the restore path that survives our process dying, and arming is the
        # gate on everything that could need it.
        snap.store_as_preset(
            device,
            baseline_snapshot,
            self.plan.scratch_slot,
            self._preset_name(),
        )

        # store_as_preset claims the working area is untouched. Verifying that
        # costs one re-read of eight records and turns a docstring into
        # evidence -- and if it is ever false, this is a run that has already
        # changed the car before measuring it.
        drifted = snap.compare(device, baseline_snapshot)
        if drifted:
            raise OrchestrationError(
                f"storing the baseline to slot {self.plan.scratch_slot} changed "
                f"the working area on outputs {sorted(drifted)}; a preset store "
                f"is supposed to touch storage only. Stop and investigate -- the "
                f"snapshot on disk is still valid."
            )

        device.arm_writes(
            reason=f"tuning run {self.plan.session_id}", evidence=evidence
        )
        # After arming, because proving isolation writes mute bits; and after
        # the snapshot, because the mute states in it are what restore()
        # returns to.
        self.isolator.begin(baseline_snapshot)

        # The transmit policy refuses writes to linked channels, and it is
        # right to by default: writing one half of a pair leaves the device
        # disagreeing with the model. A declared gang is the caller saying it
        # writes every member with the same values, which is the one case the
        # refusal is wrong for. The isolator has already checked that no gang
        # covers a link group only in part.
        for source, gang in self.plan.gang_for_source.items():
            del source
            if not gang.is_solo:
                device.session.policy.acknowledge_gang(frozenset(gang.outputs))

        # A gang whose members already disagree is a pre-existing mismatch,
        # and on drivers sharing an enclosure it may already be doing damage.
        # Checked here, before any write, because the honest options are all
        # the operator's: match them in the vendor app, drop the gang, or drop
        # those outputs from the run.
        self._require_gangs_matched(when="in the baseline, before any write")

        self._record(
            Stage.ARM,
            True,
            f"snapshot {baseline_snapshot.digest[:12]} saved to "
            f"{self.plan.snapshot_path.name}; baseline stored to preset slot "
            f"{self.plan.scratch_slot}",
            snapshot_digest=baseline_snapshot.digest,
            snapshot_path=str(self.plan.snapshot_path),
            scratch_slot=self.plan.scratch_slot,
            firmware=identity.firmware,
            linked_channels=sorted(baseline_snapshot.linked_channels()),
        )

    def _prove_isolation(self) -> None:
        """Prove by measurement that muting silences the path.

        Rule 5 in one stage. A readback says the byte we wrote is the byte
        stored; it says nothing about whether that byte silences the driver,
        or whether the microphone is hearing something that never went through
        the DSP at all. Muting everything and requiring a silent sweep is the
        only cheap check that distinguishes those.

        Costs one sweep per session. :class:`~tuner.orchestrate.isolate.
        NoIsolation` makes it a no-op, which is correct for a bench rig with
        one output wired and everything else physically disconnected -- and
        which is why that class demands a written basis.
        """
        probe = self.plan.sources[0]
        limit = self.backend.stimulus_limit(
            probe, self.plan.ceiling_for_source(probe)[0]
        )
        self.isolator.prove_silence(
            lambda: self.measurer.measure(probe, limit, "silence-proof")
        )
        described = self.isolator.describe()
        for warning in described.get("warnings", ()) or ():
            self._record(Stage.ISOLATION, True, warning, warning=True)
        self._record(
            Stage.ISOLATION,
            True,
            f"isolation by {described['method']}"
            + (
                ", proved silent by measurement"
                if described.get("proved_silent")
                else f", declared: {described.get('basis', '')}"
            ),
            **described,
        )

    def _baseline(self) -> dict[int, list[Measurement]]:
        """Take the run's opening measurements, and the baseline from them.

        The repeats are full measurement sets scored by the actual objective,
        because the floor has to be in the objective's own units to be
        compared against an improvement in them. Deriving it from one
        convenient sweep would give a number that is smaller than the real
        spread and therefore accepts changes that are noise.

        **``floor_repeats - 1`` of them happen here and the last happens after
        the fit.** Same number of sweeps, same cost, but the repeats then
        bracket the run instead of clustering in its first thirty seconds --
        see :meth:`_close_floor`.
        """
        self._floor_started_s = time.monotonic()
        self._floor_scores = []
        last: dict[int, list[Measurement]] = {}
        for i in range(self.plan.floor_repeats - 1):
            last = self._measure_all(f"floor-{i}")
            if i == 0:
                self._require_verdict_possible(last)
            self._floor_scores.append(self.plan.objective.score(last))

        baseline_score = self._floor_scores[-1]
        self._baseline_at_s = time.monotonic()
        self._record(
            Stage.BASELINE,
            True,
            f"baseline score {baseline_score:.3f} dB "
            f"(last of {self.plan.floor_repeats - 1} opening repeats; the "
            f"floor's final repeat comes after the fit)",
            score=baseline_score,
            outputs=list(self.plan.outputs),
        )
        return last

    def _close_floor(self) -> RepeatabilityFloor:
        """Take the floor's last repeat, as late as the baseline state lasts.

        Called between the fit and the write. The fit writes nothing, so the
        device still holds exactly what the opening repeats measured; the
        write is the moment that stops being true. This is therefore the
        latest honest repeat available, and it is what makes ``span_s`` cover
        the run rather than its first thirty seconds.

        Costs no extra sweeps: one of the opening repeats was moved here.
        """
        final = self._measure_all(f"floor-{self.plan.floor_repeats - 1}")
        self._floor_scores.append(self.plan.objective.score(final))
        span_s = time.monotonic() - self._floor_started_s

        floor = measure_repeatability(
            self._floor_scores, self.plan.session_id, span_s=span_s
        )
        scores = ", ".join(f"{s:.3f}" for s in self._floor_scores)
        self._record(
            Stage.FLOOR,
            True,
            f"floor {floor.value:.3f} dB from {floor.n_repeats} repeats "
            f"spanning {span_s:.0f} s (scores {scores})",
            floor_db=floor.value,
            scores=list(self._floor_scores),
            span_s=round(span_s, 1),
        )

        if floor.value == 0.0:
            # Not impossible, but on a real rig it means the repeats are not
            # independent -- a cached measurement, or a synthetic one.
            self._record(
                Stage.FLOOR,
                True,
                "floor is exactly zero, which on hardware means the repeats "
                "were not independent measurements",
                warning="zero_floor",
            )
        return floor

    def _fit(
        self, baseline: Mapping[int, Sequence[Measurement]]
    ) -> dict[int, ChannelConfig]:
        """Model-space search. The device is not consulted and not written."""
        objective = self.plan.objective
        weights = objective.position_weights
        configs: dict[int, ChannelConfig] = {}
        detail: dict[int, dict[str, object]] = {}

        for source, gang in self.plan.gang_for_source.items():
            measured_db = power_average_db(
                [m.magnitude_dbfs(objective.freqs_hz) for m in baseline[source]],
                weights,
            )
            raw_db, existing_db = self._without_existing_eq(source, measured_db)
            # Fit against the **raw** response, not the measured one. See
            # _without_existing_eq: the write replaces the channel's EQ, so
            # the response afterwards is raw + fit, and fitting `measured`
            # would count the existing EQ twice.
            target_db, _ = objective.target_for_fit_db(raw_db)
            bands = biquad.fit(
                raw_db,
                target_db,
                objective.freqs_hz,
                self.backend.limits.sample_rate_hz,
                self.plan.constraints,
            )
            # The gain change is read off the **fitted** chain, not estimated
            # before it. The fit matches shape and deliberately leaves the
            # constant to gain -- see biquad.objective -- so whatever level
            # the chain ends up sitting at is what gain has to cancel.
            #
            # Estimating it beforehand was right only while the fitter was
            # forced to match absolute level, which was the defect: a target
            # whose own mean sits below the measurement then demanded a
            # broadband boost the constraints forbid. Measuring it afterwards
            # is both correct and strictly more accurate, because it accounts
            # for what the bands actually do rather than what was hoped.
            fitted_db = raw_db + biquad.response_db(
                bands, objective.freqs_hz, self.backend.limits.sample_rate_hz
            )
            _, level_offset_db = objective.target_for_fit_db(fitted_db)
            # One fit per source, applied to every member. The gain change
            # is computed once and subtracted from each member's own live
            # gain, which is equivalent to using the leader's -- ARM already
            # refused a gang whose members disagreed -- and stays correct if
            # that check is ever relaxed to a warning.
            for output in gang.outputs:
                live = self.backend.read_channel(output)
                configs[output] = replace(
                    live,
                    gain_dbfs=live.gain_dbfs - level_offset_db,
                    peq=bands,
                )
            detail[source] = {
                "gang": list(gang.outputs),
                "label": gang.label,
                "bands": len(bands),
                "level_offset_db": round(float(level_offset_db), 3),
                "gain_dbfs": round(configs[source].gain_dbfs, 3),
                # What was subtracted before fitting, so a report can show
                # whether the channel started flat or already equalised.
                "existing_bands_removed": len(self.backend.read_channel(source).peq),
                "existing_eq_peak_db": round(float(np.max(np.abs(existing_db))), 3),
            }

        if self.plan.align_delays:
            configs = self._apply_delays(baseline, configs, detail)

        # Account the **whole device**, not just the outputs being tuned.
        # Delay RAM is pooled per chip, so an output this run never touches
        # still consumes the pool its chip-mates need -- and a budget built
        # only from `configs` would report comfortable headroom right up until
        # the write failed. Untouched outputs are read live.
        whole_device = [
            configs.get(o) or self.backend.read_channel(o)
            for o in range(self.backend.limits.n_outputs)
        ]
        usage = budget.account(whole_device, self.backend.limits)
        if not usage.fits:
            # A tune the device cannot hold is not a tune. Raised before any
            # write, which is the whole reason the budget is a constraint on
            # the fit rather than a check after it.
            over = ", ".join(
                f"output {o.output + 1} at {o.delay_samples_used}/"
                f"{o.delay_samples_available}"
                for o in usage.over_budget()
            )
            raise OrchestrationError(
                f"the fitted configuration exceeds the per-output delay "
                f"ceiling: {over}. Delay is allocated per output on this "
                f"device, so no other channel's headroom is relevant. "
                f"Nothing has been written."
            )

        self._record(
            Stage.FIT,
            True,
            f"fitted {sum(len(c.peq) for c in configs.values())} bands across "
            f"{len(configs)} outputs; {usage.summary()}",
            per_output=detail,
            delay_samples_used=usage.delay_samples_used,
            # The tightest **output**, not the device total. Delay stopped
            # being pooled per chip on 2026-08-12; the minimum is still the
            # figure that binds, for the same reason it was before.
            delay_headroom_samples=usage.delay_headroom_samples,
            delay_per_output={
                str(o.output): [o.delay_samples_used, o.delay_samples_available]
                for o in usage.outputs
            },
            budget_measured=self.backend.limits.measured,
        )
        return configs

    def _without_existing_eq(
        self, source: int, measured_db: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """The channel's response with its currently-loaded EQ modelled out.

        **The fix for a defect that made every second tune wrong.** The run
        measures a baseline with whatever EQ the channel already has, and then
        writes ``EXCLUSIVE``, which *replaces* that EQ. So the response
        afterwards is ``raw + fitted``, while a fit against ``measured`` is
        solving ``raw + existing + fitted = target``. The existing EQ is
        counted twice: once inside the measurement, and once again by being
        deleted.

        Demonstrated before this existed: a channel pre-loaded with +8 dB at
        1200 Hz landed **5.9 dB** from where the same run starting flat landed,
        and the run reported ``accepted`` both times. Every channel on the
        development car has EQ loaded, so a first real tune met it immediately.

        **Why subtract a model rather than flatten and re-measure.** Flattening
        the channel before the baseline sweep also yields a raw response, and
        it needs no model at all — but it destroys the thing the improvement
        invariant compares against. The invariant asks "is this better than
        what the operator had", and a baseline measured with the EQ flattened
        answers "is this better than no EQ", which is a different and much
        weaker claim. Keeping both would need **two sweeps per source**, and
        measurement time is one of the two real constraints in this project.
        Subtracting the model gets both from one sweep: ``measured`` stays the
        invariant's baseline, ``raw`` feeds the fit.

        **What licenses trusting the model here.** ``tune_run predict-check``
        measured this exact model against the device on 2026-08-11 — one band
        written through the production backend, swept differentially — at
        **0.065 dB rms against a 0.0585 dB repeatability floor**. The model and
        the device could not be told apart. That measurement is what turned
        this from a shortcut into the better option; before it, flattening
        would have been the defensible choice.

        Returns ``(raw_db, existing_db)``; the second is reported so a run
        record shows what was removed.
        """
        live = self.backend.read_channel(source)

        # The subtraction assumes every band `read_channel` reports is
        # actually executing. That is exactly the open question about EQ slots
        # 11-30: the record stores 31 and the vendor app exposes 10, and
        # subtracting a filter the firmware never ran would corrupt the fit
        # just as surely as not subtracting one it did.
        #
        # Guarded by count rather than by index, because the channel record is
        # not on the backend interface -- `Dsp408Spp.record` exists, the
        # simulator has no equivalent. A count above the supported ceiling is
        # the case that can actually arise, and it is refused before the fit
        # rather than at the write, so nothing is measured against a model
        # nobody can justify.
        supported = self.backend.limits.max_peq_per_channel
        if len(live.peq) > supported:
            raise OrchestrationError(
                f"source {source + 1} has {len(live.peq)} non-flat EQ bands "
                f"but this device supports {supported}. The fit subtracts the "
                f"currently-loaded EQ from the measurement, and it cannot know "
                f"whether bands beyond the supported range are executing -- "
                f"see the open question about slots 11-30. Nothing has been "
                f"written."
            )

        existing_db = biquad.response_db(
            tuple(live.peq),
            self.plan.objective.freqs_hz,
            self.backend.limits.sample_rate_hz,
        )
        return measured_db - existing_db, existing_db

    def _apply_delays(
        self,
        baseline: Mapping[int, Sequence[Measurement]],
        configs: dict[int, ChannelConfig],
        detail: dict[int, dict[str, object]],
    ) -> dict[int, ChannelConfig]:
        """Time alignment, normalised so the common offset is not spent.

        ``delay.align`` raises without a hardware loopback, and that raise is
        deliberately not caught: a run that asked for alignment on a rig that
        cannot time it should stop, not quietly skip the step and report a
        magnitude-only tune as though it were aligned.
        """
        delays = delay.align(
            {s: list(baseline[s]) for s in self.plan.sources},
            self.plan.objective.position_weights,
        )
        # Normalise across **physical** outputs, because the delay pool is
        # charged per channel: a two-driver gang spends its delay twice.
        outputs = self.plan.outputs
        per_output = {
            o: delays[s]
            for s, gang in self.plan.gang_for_source.items()
            for o in gang.outputs
        }
        ordered = [replace(configs[o], delay_samples=per_output[o]) for o in outputs]
        normalised = budget.normalize_delays(ordered)
        for output, config in zip(outputs, normalised, strict=True):
            configs[output] = config
        for source, gang in self.plan.gang_for_source.items():
            detail[source]["delay_samples"] = configs[gang.leader].delay_samples
        return configs

    def _write(self, configs: Mapping[int, ChannelConfig]) -> None:
        """One write pass. Read-modify-write and readback are the backend's."""
        device = self.backend.device
        assert self._snapshot is not None

        # Nothing should have moved between arming and writing. If it has, the
        # snapshot we would roll back to is no longer what the device holds.
        system_drift = snap.compare_system(device, self._snapshot)
        if system_drift:
            raise OrchestrationError(
                f"global device state changed between arming and writing "
                f"(blocks {sorted(system_drift)}); the baseline on disk no "
                f"longer describes this device. Nothing has been written."
            )

        # Isolation is not part of the tune, and the tune's write carries the
        # whole MISC block -- gain, delay and the mute bit together. So put
        # the mute states back first, then read each one back and copy it into
        # the configuration being written. Carrying the fit-time mute instead
        # would bake "every channel but this one is muted" into the tune.
        self.isolator.restore()

        before = device.stats.writes
        for output in self.plan.outputs:
            live_mute = self.backend.read_channel(output).muted
            self.backend.write_channel(
                output, replace(configs[output], muted=live_mute)
            )
        after = device.stats.writes

        self._require_gangs_matched(when="after the write")

        changed = snap.compare(device, self._snapshot)
        self._record(
            Stage.WRITE,
            True,
            f"{after - before} block writes across "
            f"{len(self.plan.outputs)} outputs; "
            f"{sum(len(v) for v in changed.values())} blocks now differ from "
            f"the baseline",
            block_writes=after - before,
            changed_blocks={str(k): v for k, v in changed.items()},
            gangs_matched=True,
        )

    def _require_gangs_matched(self, when: str = "after the write") -> None:
        """Every gang's members must hold the same tune, read back from the device.

        Called twice: once at ``ARM`` against the baseline, and once after the
        write. The two catch different things. The first catches a mismatch
        the run did not cause and must not paper over -- levelling it silently
        would change something nobody asked us to change, and a rollback would
        put the mismatch back anyway. The second catches a mismatch the run
        *did* cause.

        This is the check that actually protects the subwoofers, and it is
        deliberately a **readback** rather than a comparison of what we
        intended to send. Writing the same config twice and assuming both
        landed is the assumption the improvement invariant exists to distrust;
        a partial write, a refused frame or an off-by-one channel id all
        produce exactly the mismatch this catches.

        Compares :meth:`~tuner.dsp.dsp408_spp.Dsp408Spp.tuning_digest`, which
        covers gain, delay, crossover and every band -- and not ``spk_type``,
        polarity, name or mix, which legitimately differ between two outputs
        carrying the same tune. A whole-record comparison would fail on
        ``spk_type`` alone, and a check that cries wolf gets deleted.
        """
        for source, gang in self.plan.gang_for_source.items():
            if gang.is_solo:
                continue
            digests = {o: self.backend.tuning_digest(o) for o in gang.outputs}
            if len(set(digests.values())) != 1:
                raise OrchestrationError(
                    f"gang {gang.label} (outputs {list(gang.outputs)}, source "
                    f"{source}) does not hold one tune {when}: "
                    + ", ".join(f"{o}={d[:8]}" for o, d in digests.items())
                    + f". Basis for ganging them: {gang.basis}. Unmatched "
                    f"members are the failure the gang exists to prevent, so "
                    f"the run stops rather than measuring or writing past it."
                )

    def _verify(
        self,
        baseline: Mapping[int, Sequence[Measurement]],
        floor: RepeatabilityFloor,
    ) -> Verdict:
        """Re-measure and judge. The freeze is re-checked before scoring."""
        objective = self.plan.objective
        if floor.session_id != self.plan.session_id:
            raise OrchestrationError(
                f"repeatability floor was measured in session "
                f"{floor.session_id!r}, not {self.plan.session_id!r}; the floor "
                f"is a per-session quantity and must never be inherited"
            )

        result = self._measure_all("verify")

        # The interval the verdict actually spans: baseline sweep to
        # verification sweep. If the floor's repeats did not cover at least
        # that long, the floor bounds short-term noise and the comparison
        # inherits drift the floor never sampled -- which makes acceptance
        # too easy and rollback verification too strict, from one number.
        #
        # Recorded rather than enforced. Turning it into a refusal needs a
        # model of how the rig's noise grows with time, and this project has
        # no measurement of that; inventing one is exactly what its rules
        # forbid. What is honest is to say, in the report, that the floor was
        # measured over a shorter window than the claim it supports.
        interval_s = time.monotonic() - self._baseline_at_s
        if not floor.covers(interval_s):
            self._record(
                Stage.VERIFY,
                True,
                f"the floor spans {floor.span_s:.0f} s but this verdict "
                f"compares measurements {interval_s:.0f} s apart, so it does "
                f"not bound the drift between them",
                warning="floor_shorter_than_interval",
                floor_span_s=round(floor.span_s, 1),
                verdict_interval_s=round(interval_s, 1),
            )

        # The freeze is checked **here**, after the verification sweep and
        # immediately before the scores are computed -- not at stage entry.
        #
        # At stage entry it looked equivalent and was not: the verification
        # sweep is the longest operation in the run and, in a rig that prompts
        # an operator between positions, the one during which a person is
        # sitting in front of the results of the run so far. That is exactly
        # when an objective gets adjusted. A check before the sweep leaves the
        # whole sweep as a window; a check here leaves none, because nothing
        # runs between it and score().
        objective.require_unchanged(self._frozen_objective)
        if self.plan.fingerprint() != self._frozen_plan:
            raise OrchestrationError(
                f"the plan changed during the run: frozen as "
                f"{self._frozen_plan[:12]}, now {self.plan.fingerprint()[:12]}"
            )

        baseline_score = objective.score(baseline)
        result_score = objective.score(result)

        # Global device state is checked before the scores are compared, not
        # after. A master volume that moved between baseline and verification
        # makes the comparison say nothing about the tune -- which is
        # indeterminate, and would otherwise be reported as whichever way the
        # level happened to push the score.
        assert self._snapshot is not None
        system_drift = snap.compare_system(self.backend.device, self._snapshot)
        if system_drift:
            verdict = Verdict(
                outcome=Outcome.INDETERMINATE,
                baseline_score=baseline_score,
                result_score=result_score,
                floor=floor.value,
                reason=(
                    f"global device state changed during the run (system blocks "
                    f"{sorted(system_drift)}, master volume among them); the "
                    f"baseline and verification measurements are not comparable "
                    f"and the score difference says nothing about the tune"
                ),
            )
        else:
            representative_baseline, representative_result = self._representative(
                baseline, result
            )
            verdict = verify(
                baseline=representative_baseline,
                result=representative_result,
                baseline_score=baseline_score,
                result_score=result_score,
                objective=objective.to_verify_objective(),
                floor=floor,
            )

        self._record(
            Stage.VERIFY,
            verdict.outcome is not Outcome.INDETERMINATE,
            verdict.reason,
            baseline_score=baseline_score,
            result_score=result_score,
            delta=verdict.delta,
            floor_db=floor.value,
            outcome=verdict.outcome.value,
        )
        return verdict

    def _settle(
        self,
        verdict: Verdict,
        floor: RepeatabilityFloor,
        baseline: Mapping[int, Sequence[Measurement]],
    ) -> RunReport:
        """Accept, or roll back and prove the rollback by re-measurement."""
        if not verdict.requires_rollback:
            # The verification campaign left the last-swept output isolated.
            self.isolator.restore()
            self.backend.device.disarm()
            self._record(
                Stage.SETTLE,
                True,
                f"accepted; the tune stands. The baseline remains in preset "
                f"slot {self.plan.scratch_slot} and at "
                f"{self.plan.snapshot_path.name}",
            )
            return self._report(verdict=verdict, floor=floor)

        report, acoustic = self._rollback(verdict.baseline_score, floor, baseline)
        return self._report(
            verdict=verdict,
            floor=floor,
            rollback=report,
            rollback_verified_acoustically=acoustic,
        )

    # -- rollback ----------------------------------------------------------

    def _rollback(
        self,
        baseline_score: float,
        floor: RepeatabilityFloor,
        baseline: Mapping[int, Sequence[Measurement]],
    ) -> tuple[snap.PresetRestoreReport | snap.RestoreReport, bool]:
        """Preset recall first, block-by-block second, re-measurement always.

        The preset recall is tried first because it replaces all eight
        channels in one sequence and needs none of our encoding to be correct.
        The block restore is the fallback rather than the primary, because it
        writes -- and a rollback that writes is a rollback that can fail the
        same way the tune did.
        """
        del baseline  # kept in the signature: the acoustic check is the point
        assert self._snapshot is not None
        device = self.backend.device

        report: snap.PresetRestoreReport | snap.RestoreReport = (
            snap.restore_from_preset(
                device,
                self.plan.scratch_slot,
                expect=self._snapshot,
                dry_run=False,
            )
        )
        path = f"preset slot {self.plan.scratch_slot}"

        if not report.device_matches:
            block_report = snap.restore(
                device,
                self._snapshot,
                dry_run=False,
                reason=f"rollback for run {self.plan.session_id}",
            )
            report = block_report
            path = "block-by-block restore after the preset recall left differences"
            if not block_report.device_matches:
                self._record(
                    Stage.SETTLE,
                    False,
                    f"both restore paths failed: {report.summary()}",
                )
                raise RollbackFailed(
                    f"the device does not match the baseline after a preset "
                    f"recall from slot {self.plan.scratch_slot} and a "
                    f"block-by-block restore. {block_report.summary()} "
                    f"Remaining routes are manual: load a .DDP through the "
                    f"vendor app, or recall the operator's own base preset. "
                    f"The baseline is at {self.plan.snapshot_path}.",
                    self._report(rollback=report, floor=floor),
                )

        # Bytes are back. That is not the same as the system being back.
        #
        # The re-measurement runs under the *same* isolation the baseline
        # used, per output -- otherwise it would be comparing a score taken
        # with one driver playing against one taken with all of them. That
        # means it writes mute bits, so isolation is put back afterwards and
        # the byte comparison is redone. Only then is the device provably
        # where it started.
        restored = self._measure_all("rollback")
        restored_score = self.plan.objective.score(restored)
        drift = abs(restored_score - baseline_score)
        acoustic_ok = drift <= floor.value

        self.isolator.restore()
        left_differing = snap.compare(device, self._snapshot)
        if left_differing:
            raise RollbackFailed(
                f"the rollback re-measurement left outputs "
                f"{sorted(left_differing)} differing from the baseline; the "
                f"isolation mute states did not come back. The device is not "
                f"where the run found it. Baseline is at "
                f"{self.plan.snapshot_path} and in preset slot "
                f"{self.plan.scratch_slot}.",
                self._report(rollback=report, floor=floor),
            )

        self._record(
            Stage.SETTLE,
            acoustic_ok,
            f"rolled back via {path}; re-measured score {restored_score:.3f} dB "
            f"against baseline {baseline_score:.3f} dB "
            f"({'within' if acoustic_ok else 'OUTSIDE'} the "
            f"{floor.value:.3f} dB floor)",
            restore_path=path,
            isolation_restored=True,
            restored_score=restored_score,
            baseline_score=baseline_score,
            drift_db=drift,
            acoustic_ok=acoustic_ok,
        )

        if not acoustic_ok:
            raise RollbackFailed(
                f"the settings were restored byte-for-byte but the system did "
                f"not come back: re-measured {restored_score:.3f} dB against a "
                f"baseline of {baseline_score:.3f} dB, a drift of {drift:.3f} dB "
                f"against a session floor of {floor.value:.3f} dB. Either "
                f"something physical changed during the run -- the microphone "
                f"moved, a door opened, the cabin warmed -- or the restore did "
                f"not do what its readback says. Do not start another run "
                f"until this is understood.",
                self._report(
                    rollback=report, floor=floor, rollback_verified_acoustically=False
                ),
            )

        self.backend.device.disarm()
        return report, True

    def _abort(self, exc: Exception) -> RunReport:
        """A stage failed. Roll back if anything could have been written."""
        detail = f"{type(exc).__name__}: {exc}"
        if self._snapshot is None:
            # Nothing armed, nothing written, nothing to undo.
            self._record(Stage.SETTLE, False, f"aborted before arming: {detail}")
            return self._report(error=detail)

        report, acoustic = self._rollback_after_abort()
        return self._report(
            error=detail,
            rollback=report,
            rollback_verified_acoustically=acoustic,
        )

    def _rollback_after_abort(
        self,
    ) -> tuple[snap.PresetRestoreReport | snap.RestoreReport, bool | None]:
        """Restore after a mid-run failure, without an acoustic check.

        Deliberately byte-only. An abort can be *caused* by the measurement
        path being broken, and in that case a re-measurement is not evidence
        of anything -- so the report says ``None`` rather than claiming a
        verification it did not make. The distinction matters: ``None`` is
        "not checked", ``False`` would be "checked and failed".
        """
        assert self._snapshot is not None
        device = self.backend.device
        report: snap.PresetRestoreReport | snap.RestoreReport = (
            snap.restore_from_preset(
                device, self.plan.scratch_slot, expect=self._snapshot, dry_run=False
            )
        )
        if not report.device_matches:
            report = snap.restore(
                device,
                self._snapshot,
                dry_run=False,
                reason=f"abort rollback for run {self.plan.session_id}",
            )
        if not report.device_matches:
            self._record(
                Stage.SETTLE, False, f"abort rollback failed: {report.summary()}"
            )
            raise RollbackFailed(
                f"a stage failed and neither restore path brought the device "
                f"back. {report.summary()} The baseline is at "
                f"{self.plan.snapshot_path} and in preset slot "
                f"{self.plan.scratch_slot}.",
                self._report(rollback=report),
            )
        device.disarm()
        self._record(
            Stage.SETTLE,
            True,
            f"aborted and rolled back to preset slot {self.plan.scratch_slot}; "
            f"the rollback was NOT verified acoustically, because the abort may "
            f"be the measurement path itself",
            restore_path="preset",
        )
        return report, None

    # -- helpers -----------------------------------------------------------

    def _measure_all(self, tag: str) -> dict[int, list[Measurement]]:
        """Sweep every tuned output, at a ceiling read back from the device."""
        objective = self.plan.objective
        out: dict[int, list[Measurement]] = {}
        for source, gang in self.plan.gang_for_source.items():
            # The whole gang goes audible together -- two drivers in one
            # enclosure are one acoustic source and there is nothing to
            # separate. Before the ceiling, because isolation is a device
            # write and the ceiling must be read after anything that changes.
            self.isolator.isolate(gang.outputs)
            driver_ceiling, _ = self.plan.ceiling_for_source(source)
            # The ceiling is read from the gang's leader but applies to the
            # whole gang: ceiling_for_source already took the minimum across
            # members, so a gang is never louder than its most fragile driver
            # would allow on its own.
            limit = self.backend.stimulus_limit(source, driver_ceiling)
            measurements = list(self.measurer.measure(source, limit, tag))
            if len(measurements) != objective.n_positions:
                raise ObjectiveError(
                    f"{tag}: source {source} ({gang.label}) returned "
                    f"{len(measurements)} measurements but the objective "
                    f"weights {objective.n_positions} positions"
                )
            out[source] = measurements
        return out

    def _require_verdict_possible(
        self, measured: Mapping[int, Sequence[Measurement]]
    ) -> None:
        """Stop now if no verification could ever be comparable to this.

        Run against the **first** measurement set of the run, before the fit
        and long before the write. It asks each measurement's provenance
        whether it is comparable to itself, which sounds vacuous and is not:
        every pairwise term cancels, leaving only the structural requirements
        -- an acoustic measurement needs a setup token and a temperature.

        The alternative is what actually happened on 2026-08-12, the first
        time the loop met hardware: the run armed, measured, fitted, wrote
        eleven blocks, and *then* discovered at VERIFY that no thermometer
        reading had been supplied, so the verdict was indeterminate and the
        device had to be rolled back. Nothing was wrong with the tune. The
        run simply could not have said so, and it could have known that
        before it changed anything.
        """
        for source in sorted(measured):
            for m in measured[source]:
                reason = m.provenance.why_incomparable(m.provenance)
                if reason is None:
                    continue
                raise OrchestrationError(
                    f"this run can never reach a verdict: {reason}. The "
                    f"baseline and the verification sweep would be "
                    f"incomparable however good the tune is, so the run "
                    f"stops here rather than fitting and writing first. "
                    f"Nothing has been written."
                )

    @staticmethod
    def _representative(
        baseline: Mapping[int, Sequence[Measurement]],
        result: Mapping[int, Sequence[Measurement]],
    ) -> tuple[Measurement, Measurement]:
        """A baseline/result pair for ``verify`` to check provenance on.

        Prefers the **first incomparable** pair, so that one drifted
        measurement anywhere in the set produces INDETERMINATE rather than
        being averaged into silence by a comparable representative.
        """
        first: tuple[Measurement, Measurement] | None = None
        for output in sorted(baseline):
            for b, r in zip(baseline[output], result[output], strict=True):
                if first is None:
                    first = (b, r)
                if not b.provenance.comparable_to(r.provenance):
                    return b, r
        assert first is not None
        return first

    def _preset_name(self) -> str:
        """A slot name that says what it is, inside the wire's 15 characters."""
        return f"bl {self.plan.session_id}"[:PRESET_NAME_LEN]

    def _record(self, stage: Stage, ok: bool, detail: str, **data: object) -> None:
        self._stages.append(StageRecord(stage=stage, ok=ok, detail=detail, data=data))

    def _report(self, **kwargs: object) -> RunReport:
        return RunReport(
            plan_fingerprint=self._frozen_plan,
            objective_fingerprint=self._frozen_objective,
            stages=tuple(self._stages),
            plan=self.plan.as_dict(),
            **kwargs,  # type: ignore[arg-type]
        )
