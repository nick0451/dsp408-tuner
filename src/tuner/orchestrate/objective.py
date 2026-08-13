"""The scalar a run is judged by, and the machinery that freezes it.

``verify.Objective`` records *that* an objective was chosen and how the seats
were weighted. It does not compute anything. This module supplies the missing
half -- an actual score over measurements -- and, more importantly, makes the
freeze checkable.

Why the freeze needs enforcing rather than documenting
------------------------------------------------------
The improvement invariant's first qualification is that the objective and its
weights are fixed before the run. The failure it guards against is not
dishonesty; it is the entirely natural act of looking at a disappointing
result, noticing the driver's seat did improve, and re-weighting toward it.
Every individual step of that still looks honest, and the run still reports
"improved by more than the floor".

A comment saying "frozen" does not stop it. A fingerprint taken at plan time
and re-checked before the verdict does, because re-weighting changes the hash
and :class:`ObjectiveChanged` names the field that moved.

Why the score is level-invariant
--------------------------------
:meth:`MagnitudeObjective.score` measures **shape**: rms deviation from the
target after the target has been raised to the measurement's own level, which
is exactly the split :func:`tuner.optimize.target.correction_db` makes.

That is a real limitation and it is deliberate. Our magnitude is in dBFS, not
dB SPL -- the microphone calibration that would make an absolute level
meaningful is ``tuner.cal``, which is not built. An objective that scored
absolute level would therefore be scoring the interface's input gain, which
changes between sessions and is precisely what the provenance rules refuse to
compare. Shape is what we can measure honestly today.

The consequence, stated so no report overclaims: **a run cannot detect or
reward an overall level error.** Channel gain is set from
``level_offset_db`` and its correctness is not evidenced by the verdict.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from ..measure.result import Measurement
from ..optimize.target import DEFAULT_LEVEL_BAND_HZ, TargetCurve, correction_db
from ..optimize.verify import Objective


class ObjectiveChanged(RuntimeError):
    """The objective moved between the plan and the verdict.

    Raised rather than warned about, because a changed objective does not
    degrade the verdict -- it voids it. The comparison that would be reported
    is between a baseline scored one way and a result scored another.
    """


class ObjectiveError(ValueError):
    """The objective is malformed."""


def _frozen(values: Sequence[float] | np.ndarray, what: str) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 1 or array.size == 0:
        raise ObjectiveError(f"{what} must be a non-empty 1-D array")
    if not np.all(np.isfinite(array)):
        raise ObjectiveError(f"{what} contains non-finite values")
    array.setflags(write=False)
    return array


@dataclass(frozen=True)
class MagnitudeObjective:
    """Weighted rms deviation from a target curve, in dB. Lower is better.

    Attributes:
        name: Recorded in provenance and quoted in the verdict's reason.
        target: The curve being matched. Relative; see the module docstring.
        freqs_hz: The **log-spaced** axis every magnitude is evaluated on.
            Stated explicitly rather than passed as a bare array, per the
            frequency-axis convention: a uniform mean over a log axis weights
            each octave equally, which is what matching by ear rewards.
        band_hz: The scored band. Points outside it are ignored entirely --
            not down-weighted -- because a channel's stopband contains no
            information about the tune and a great deal of noise.
        source_weights: Relative importance of each **source**. A source is
            one thing that is measured once and corrected once, keyed by its
            lowest output index. Usually that is a single output; a gang of
            drivers sharing an enclosure is one source spanning several, and
            the membership lives in :class:`~tuner.orchestrate.plan.TunePlan`
            rather than here, because it is a fact about the car and not about
            the objective.
        position_weights: Relative importance of each listening position, in
            the order measurements are supplied. A multi-seat tune has
            conflicting optima; this is where the compromise is made explicit
            rather than hidden in a default.
        level_band_hz: Band over which the target is level-matched to the
            measurement before the deviation is taken. Kept separate from
            ``band_hz`` so a run can score a wide band while anchoring level
            on the midrange, which is where a level match is least ambiguous.
    """

    name: str
    target: TargetCurve
    freqs_hz: np.ndarray
    source_weights: Mapping[int, float]
    position_weights: tuple[float, ...] = (1.0,)
    band_hz: tuple[float, float] = (30.0, 16_000.0)
    level_band_hz: tuple[float, float] = DEFAULT_LEVEL_BAND_HZ
    lower_is_better: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        if not self.name:
            raise ObjectiveError("an objective needs a name; it goes in provenance")
        object.__setattr__(self, "freqs_hz", _frozen(self.freqs_hz, "freqs_hz"))
        if np.any(np.diff(self.freqs_hz) <= 0):
            raise ObjectiveError("freqs_hz must be strictly ascending")

        lo, hi = self.band_hz
        if not 0 < lo < hi:
            raise ObjectiveError(f"bad band_hz {self.band_hz}")
        if not np.any(self._mask()):
            raise ObjectiveError(
                f"band {lo:g}-{hi:g} Hz contains none of the "
                f"{self.freqs_hz.size} axis points; the score would be empty"
            )

        if not self.source_weights:
            raise ObjectiveError("no sources weighted; nothing would be scored")
        if any(w <= 0 for w in self.source_weights.values()):
            raise ObjectiveError("source weights must be positive")
        object.__setattr__(
            self,
            "source_weights",
            {int(k): float(v) for k, v in sorted(self.source_weights.items())},
        )

        if not self.position_weights:
            raise ObjectiveError("no positions weighted")
        if any(w <= 0 for w in self.position_weights):
            raise ObjectiveError("position weights must be positive")
        object.__setattr__(
            self,
            "position_weights",
            tuple(float(w) for w in self.position_weights),
        )

    # -- geometry ----------------------------------------------------------

    @property
    def sources(self) -> tuple[int, ...]:
        """Leader output of each thing being scored, ascending."""
        return tuple(self.source_weights)

    @property
    def n_positions(self) -> int:
        return len(self.position_weights)

    def _mask(self) -> np.ndarray:
        lo, hi = self.band_hz
        return (self.freqs_hz >= lo) & (self.freqs_hz <= hi)

    # -- scoring -----------------------------------------------------------

    def target_for_fit_db(self, measured_db: np.ndarray) -> tuple[np.ndarray, float]:
        """The level-matched target, and the gain change to apply separately.

        Thin pass-through to :func:`tuner.optimize.target.correction_db`, kept
        here so the fitter and the scorer are provably given the same target.
        A run that fitted against one curve and scored against another would
        report an honest-looking verdict about a fit nobody made.
        """
        return correction_db(
            np.asarray(measured_db, dtype=np.float64),
            self.target,
            self.freqs_hz,
            self.level_band_hz,
        )

    def deviation_db(self, measurement: Measurement) -> np.ndarray:
        """Per-point residual against the level-matched target, in dB."""
        measured_db = measurement.magnitude_dbfs(self.freqs_hz)
        target_db, _ = self.target_for_fit_db(measured_db)
        return measured_db - target_db

    def score_one(self, measurement: Measurement) -> float:
        """Rms deviation for a single measurement, over the scored band."""
        residual = self.deviation_db(measurement)[self._mask()]
        return float(np.sqrt(np.mean(residual**2)))

    def score(self, measurements: Mapping[int, Sequence[Measurement]]) -> float:
        """The run's scalar: outputs and positions combined by weight.

        ``measurements`` maps **source** index to that source's measurement
        at each listening position, in the order ``position_weights``
        describes. A gang is measured once, as one source, because two drivers
        in one enclosure are one acoustic thing at the listening position.

        Combining is done on **mean squares, then one square root at the end**
        rather than by averaging rms values. Averaging rms understates a
        response that is excellent at three seats and terrible at the fourth,
        which is the compromise the weights exist to make visible.
        """
        missing = set(self.source_weights) - set(measurements)
        if missing:
            raise ObjectiveError(
                f"objective {self.name!r} weights sources "
                f"{sorted(self.source_weights)} but no measurement was given "
                f"for {sorted(missing)}"
            )
        extra = set(measurements) - set(self.source_weights)
        if extra:
            raise ObjectiveError(
                f"measurements supplied for unweighted sources {sorted(extra)}; "
                f"scoring them would silently change the objective"
            )

        pos_w = np.asarray(self.position_weights, dtype=np.float64)
        mask = self._mask()

        total = 0.0
        total_weight = 0.0
        for source, weight in self.source_weights.items():
            per_position = measurements[source]
            if len(per_position) != self.n_positions:
                raise ObjectiveError(
                    f"source {source} has {len(per_position)} measurements but "
                    f"the objective weights {self.n_positions} positions"
                )
            for w, m in zip(pos_w, per_position, strict=True):
                residual = self.deviation_db(m)[mask]
                total += weight * w * float(np.mean(residual**2))
                total_weight += weight * w
        return float(np.sqrt(total / total_weight))

    # -- the freeze --------------------------------------------------------

    def as_dict(self) -> dict[str, object]:
        """Canonical, JSON-safe form. This is what gets hashed and recorded."""
        return {
            "name": self.name,
            "kind": "magnitude_rms_db",
            "lower_is_better": self.lower_is_better,
            "target_name": self.target.name,
            "target_freqs_hz": [round(f, 6) for f in self.target.freqs_hz.tolist()],
            "target_magnitude_db": [
                round(v, 6) for v in self.target.magnitude_db.tolist()
            ],
            "freqs_hz": [round(f, 6) for f in self.freqs_hz.tolist()],
            "band_hz": [float(self.band_hz[0]), float(self.band_hz[1])],
            "level_band_hz": [
                float(self.level_band_hz[0]),
                float(self.level_band_hz[1]),
            ],
            "source_weights": {str(k): v for k, v in self.source_weights.items()},
            "position_weights": list(self.position_weights),
        }

    def fingerprint(self) -> str:
        """SHA-256 of :meth:`as_dict`. Taken at plan time, checked at verdict.

        Every field that could change the score is in the hash, including the
        target's own points -- a target edited in place between the baseline
        and the verification would otherwise be invisible.
        """
        blob = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def require_unchanged(self, fingerprint: str) -> None:
        """Raise unless this objective still hashes to ``fingerprint``.

        The error names the fields that differ, because "the objective
        changed" with no further detail is the kind of message that gets
        suppressed rather than investigated.
        """
        current = self.fingerprint()
        if current == fingerprint:
            return
        raise ObjectiveChanged(
            f"objective {self.name!r} was frozen as {fingerprint[:12]} and now "
            f"hashes to {current[:12]}; the verdict it would produce compares a "
            f"baseline scored one way against a result scored another. "
            f"Re-run from the baseline with the new objective, or restore the "
            f"old one -- do not report this comparison."
        )

    def to_verify_objective(self) -> Objective:
        """The provenance record ``optimize.verify`` takes."""
        return Objective(
            name=self.name,
            position_weights=self.position_weights,
            lower_is_better=self.lower_is_better,
        )
