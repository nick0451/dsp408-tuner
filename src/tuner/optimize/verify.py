"""Enforcement of the improvement invariant.

No tune is accepted unless a fresh measurement shows it improved the objective
by more than the measurement repeatability floor. See CLAUDE.md.

The optimizer's predicted improvement is not evidence and is not accepted here.
Only a physical re-measurement taken after the settings were written can accept
a result -- an optimizer converging against a model of a system it has stopped
resembling is the default failure mode of this class of tool.

The decision logic below is deliberately separable from the measurement and
rollback machinery so it can be tested exhaustively without hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from ..measure.result import Measurement


class Outcome(Enum):
    """Result of a verification. Three outcomes, not two.

    INDETERMINATE exists because a comparison across incomparable provenance
    is not a failure -- it is an absence of evidence. Collapsing it into
    either bucket is how a temperature-driven change gets recorded as a
    tuning success.
    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class Objective:
    """The scalar being optimized, frozen before the run.

    Freezing matters: if the objective or its weighting may be chosen after
    seeing results, "improved" becomes trivially satisfiable by re-weighting,
    and every individual step still looks honest. This object is recorded in
    the run's provenance.

    Attributes:
        name: Identifier recorded in run provenance.
        position_weights: Relative importance of each listening position. A
            multi-seat tune has conflicting optima; this is where the
            compromise is made explicit rather than hidden in a default.
        lower_is_better: Whether the score is a cost (typical) or a merit.
    """

    name: str
    position_weights: tuple[float, ...]
    lower_is_better: bool = True


@dataclass(frozen=True)
class RepeatabilityFloor:
    """Session-specific measurement noise floor.

    Established by repeating one measurement several times without changing
    anything; the spread is the floor. This is a per-session quantity -- it
    moves with temperature stability, mounting and ambient noise -- so it is
    measured each session and never carried over from a previous one.

    Attributes:
        value: The floor, in the objective's units.
        n_repeats: How many repeat measurements it was derived from.
        session_id: Ties the floor to the session that measured it.
        span_s: Wall-clock seconds from the first repeat to the last. **A
            floor without a timescale is not a floor**; see below.
    """

    value: float
    n_repeats: int
    session_id: str
    span_s: float = 0.0

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("repeatability floor must be non-negative")
        if self.n_repeats < 2:
            raise ValueError(
                "repeatability floor needs at least 2 repeat measurements; "
                f"got {self.n_repeats}"
            )
        if self.span_s < 0:
            raise ValueError("a floor's span cannot be negative")

    def covers(self, interval_s: float, min_fraction: float = 0.5) -> bool:
        """Whether these repeats spanned enough of the comparison they judge.

        Repeats taken back to back measure short-term noise and are **blind to
        drift**. Both halves of the improvement invariant compare a baseline
        against a measurement taken minutes later, so both inherit whatever
        the rig did in between -- and a floor that never sampled that window
        cannot bound it.

        It is wrong in both directions at once, which is why it is not simply
        conservative. Acceptance requires beating the baseline *by more than*
        the floor, so a floor measured too short makes acceptance **too easy**.
        Rollback verification requires matching the baseline *within* the
        floor, so the same number makes unwinding **too strict**.

        Observed 2026-08-12, first time the loop met hardware: three
        back-to-back sweeps gave 0.003 dB, and the score had moved 0.006 dB by
        the time the rollback was checked. Twice the floor, so ``RollbackFailed``
        -- on a device that was byte-identical to its snapshot. Nothing was
        wrong with the restore. The rig had drifted over a window the floor
        never sampled.

        **Why a fraction and not equality.** Equality is unreachable by
        construction: the verification sweep is the thing being judged, so it
        is always after the last repeat that could be taken with the device
        still holding the baseline. Requiring ``span_s >= interval_s`` fires
        on every run by roughly one sweep, and a warning that always fires is
        a warning nobody reads.

        ``min_fraction`` is therefore a **reporting heuristic and not a
        measured quantity** -- say so wherever it is quoted. A half leaves
        room for the one measurement round that can never be covered, out of
        a run containing several, while still catching the case that actually
        happened, where the floor covered an eighth of its interval.
        """
        if interval_s <= 0:
            return True
        return self.span_s >= min_fraction * interval_s


@dataclass(frozen=True)
class Verdict:
    """Outcome of comparing a candidate tune against its baseline."""

    outcome: Outcome
    baseline_score: float
    result_score: float
    floor: float
    reason: str

    @property
    def delta(self) -> float:
        """Change in score. Sign is objective-dependent."""
        return self.result_score - self.baseline_score

    @property
    def requires_rollback(self) -> bool:
        """Both rejection and indeterminacy roll back.

        A tuning run that does not demonstrably improve the system must leave
        it no worse than it found it.
        """
        return self.outcome is not Outcome.ACCEPTED


def measure_repeatability(
    scores: list[float],
    session_id: str,
    span_s: float = 0.0,
) -> RepeatabilityFloor:
    """Derive the session's noise floor from repeated identical measurements.

    Uses the full spread rather than a standard deviation: with the small
    repeat counts practical in a car, the spread is the more honest bound on
    what a single measurement might be off by.

    ``span_s`` is the wall-clock from the first repeat to the last, and it is
    part of the answer rather than metadata -- see :meth:`
    RepeatabilityFloor.covers`. Repeats taken back to back measure short-term
    noise; the invariant they serve compares measurements minutes apart.
    """
    if len(scores) < 2:
        raise ValueError("need at least 2 repeat measurements")
    return RepeatabilityFloor(
        value=float(np.max(scores) - np.min(scores)),
        n_repeats=len(scores),
        session_id=session_id,
        span_s=float(span_s),
    )


def verify(
    baseline: Measurement,
    result: Measurement,
    baseline_score: float,
    result_score: float,
    objective: Objective,
    floor: RepeatabilityFloor,
) -> Verdict:
    """Decide whether a candidate tune may be accepted.

    ``result`` must be a fresh measurement taken after the candidate settings
    were written to the DSP. Passing a predicted or simulated response here
    defeats the invariant.

    Provenance is checked first: an incomparable pair yields INDETERMINATE
    rather than a verdict, because the comparison carries no information about
    the tune.
    """
    incomparable = baseline.provenance.why_incomparable(result.provenance)
    if incomparable is not None:
        return Verdict(
            outcome=Outcome.INDETERMINATE,
            baseline_score=baseline_score,
            result_score=result_score,
            floor=floor.value,
            # Name the term. This verdict is reported after a run that has
            # already spent several minutes and changed the device, and
            # "incomparable provenance" alone leaves the operator to guess
            # which of six fields moved.
            reason=(
                f"baseline and verification measurements are incomparable: "
                f"{incomparable}. The comparison says nothing about the tune"
            ),
        )

    improvement = (
        baseline_score - result_score
        if objective.lower_is_better
        else result_score - baseline_score
    )

    if improvement > floor.value:
        return Verdict(
            outcome=Outcome.ACCEPTED,
            baseline_score=baseline_score,
            result_score=result_score,
            floor=floor.value,
            reason=(
                f"objective '{objective.name}' improved by {improvement:.3f}, "
                f"exceeding the session repeatability floor {floor.value:.3f}"
            ),
        )

    return Verdict(
        outcome=Outcome.REJECTED,
        baseline_score=baseline_score,
        result_score=result_score,
        floor=floor.value,
        reason=(
            f"objective '{objective.name}' changed by {improvement:+.3f}, "
            f"which does not exceed the session repeatability floor "
            f"{floor.value:.3f}; this is indistinguishable from measurement "
            f"noise"
        ),
    )
