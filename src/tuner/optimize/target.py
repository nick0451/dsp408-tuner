"""Target response curves.

A car interior is not a listening room, and a flat in-car measured response
sounds bright and thin. Target curves encode the expected downward tilt --
Harman's in-car research is the usual starting point.

The target is deliberately a plain data structure rather than something
learned. A preference-learned target curve is a documented phase-2 extension
(see CLAUDE.md), and the interface here is what it would plug into: anything
producing a magnitude-vs-frequency array is a valid target, learned or not.

Level is not part of the target
-------------------------------
A target describes **shape**, not level. Absolute level is set by channel gain,
and conflating the two is expensive: a target sitting 20 dB above the measured
curve would have the fitter spend every band it has on broadband boost, running
out of resources before it corrects anything that matters -- and asking for
gain the device may not have.

So a target is normalised, and :func:`correction_db` removes the level
difference before returning what the EQ should actually do. The offset it
removed is reported separately, because that number is the channel gain change
the tune needs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

#: Band over which a target is levelled, and over which measured and target are
#: matched before fitting. Chosen to sit above cabin gain and below the region
#: where seat-to-seat variation dominates -- both of which move the average
#: without saying anything about the shape being corrected.
DEFAULT_LEVEL_BAND_HZ = (200.0, 4000.0)


class TargetError(ValueError):
    """Raised when a target curve is malformed or used outside its range."""


@dataclass(frozen=True)
class TargetCurve:
    """Desired magnitude response.

    Attributes:
        freqs_hz: Frequency points, strictly ascending. Need not be log-spaced,
            but interpolation is done in log frequency, so a sparse curve
            behaves the way a plot of it would look.
        magnitude_db: Target magnitude, **relative** -- absolute level is set
            separately by system gain.
        name: Human-readable identifier for logs and reports.
    """

    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    name: str = "custom"

    def __post_init__(self) -> None:
        freqs = np.asarray(self.freqs_hz, dtype=np.float64)
        mags = np.asarray(self.magnitude_db, dtype=np.float64)
        if freqs.ndim != 1 or freqs.size < 2:
            raise TargetError("a target needs at least two frequency points")
        if freqs.shape != mags.shape:
            raise TargetError(
                f"freqs_hz {freqs.shape} and magnitude_db {mags.shape} differ"
            )
        if np.any(freqs <= 0):
            raise TargetError("frequencies must be positive")
        if np.any(np.diff(freqs) <= 0):
            raise TargetError("frequencies must be strictly ascending")
        if not np.all(np.isfinite(mags)):
            raise TargetError("magnitude_db contains non-finite values")
        object.__setattr__(self, "freqs_hz", freqs)
        object.__setattr__(self, "magnitude_db", mags)

    @property
    def range_hz(self) -> tuple[float, float]:
        return float(self.freqs_hz[0]), float(self.freqs_hz[-1])

    def at(self, freqs_hz: np.ndarray, clamp: bool = True) -> np.ndarray:
        """Interpolate onto a different frequency axis.

        Interpolation is linear in dB against **log** frequency, which is how
        the curve would be read off a plot.

        Outside the defined range the endpoint value is held. Extrapolating a
        tilt is the tempting alternative and it is wrong: a −3 dB/decade slope
        continued below 20 Hz asks for boost the drivers cannot make and the
        amplifier should not attempt. Pass ``clamp=False`` to raise instead,
        for callers that would rather know their axis exceeds their target.
        """
        freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
        if freqs_hz.size == 0:
            raise TargetError("freqs_hz is empty")
        if np.any(freqs_hz <= 0):
            raise TargetError("frequencies must be positive")

        lo, hi = self.range_hz
        if not clamp and (freqs_hz.min() < lo or freqs_hz.max() > hi):
            raise TargetError(
                f"axis spans {freqs_hz.min():.1f}-{freqs_hz.max():.1f} Hz but "
                f"target {self.name!r} is only defined over {lo:.1f}-{hi:.1f} Hz"
            )
        return np.interp(
            np.log(freqs_hz),
            np.log(self.freqs_hz),
            self.magnitude_db,
        )

    def normalized(
        self, band_hz: tuple[float, float] = DEFAULT_LEVEL_BAND_HZ
    ) -> TargetCurve:
        """The same shape, with its mean over ``band_hz`` moved to 0 dB."""
        offset = _band_mean(self.freqs_hz, self.magnitude_db, band_hz)
        return TargetCurve(
            freqs_hz=self.freqs_hz,
            magnitude_db=self.magnitude_db - offset,
            name=self.name,
        )

    def shifted(self, offset_db: float) -> TargetCurve:
        return TargetCurve(
            freqs_hz=self.freqs_hz,
            magnitude_db=self.magnitude_db + offset_db,
            name=self.name,
        )


def from_points(
    points: Sequence[tuple[float, float]], name: str = "custom"
) -> TargetCurve:
    """Build a target from sparse ``(frequency_hz, magnitude_db)`` pairs.

    The intended way to enter a published or measured curve: type the points
    off the plot, let interpolation fill in between.
    """
    if len(points) < 2:
        raise TargetError("need at least two points")
    ordered = sorted(points, key=lambda p: p[0])
    return TargetCurve(
        freqs_hz=np.array([p[0] for p in ordered], dtype=np.float64),
        magnitude_db=np.array([p[1] for p in ordered], dtype=np.float64),
        name=name,
    )


def flat(freqs_hz: np.ndarray, name: str = "flat") -> TargetCurve:
    """A flat target. Correct for an electrical loopback, wrong for a car."""
    freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
    return TargetCurve(freqs_hz, np.zeros_like(freqs_hz), name)


def tilted(
    freqs_hz: np.ndarray,
    tilt_db_per_decade: float = -3.0,
    pivot_hz: float = 1000.0,
) -> TargetCurve:
    """A constant-tilt target, pivoting at ``pivot_hz``.

    Useful as a baseline and for testing. **Real in-car targets are not a
    straight line** -- they typically add low-bass lift and a treble shelf, and
    a straight tilt continued to the band edges asks for more bass boost than
    any door speaker will give.

    The default of −3 dB/decade is a mild, widely used room slope, offered as a
    starting point rather than as a recommendation. It is not the Harman curve;
    see :func:`harman_in_car`.
    """
    freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
    if freqs_hz.size < 2:
        raise TargetError("need at least two frequency points")
    if pivot_hz <= 0:
        raise TargetError("pivot_hz must be positive")
    magnitude = tilt_db_per_decade * np.log10(freqs_hz / pivot_hz)
    return TargetCurve(
        freqs_hz=freqs_hz,
        magnitude_db=magnitude,
        name=f"tilt {tilt_db_per_decade:+.1f} dB/decade @ {pivot_hz:.0f} Hz",
    )


def harman_in_car(freqs_hz: np.ndarray) -> TargetCurve:
    """Harman-style in-car target.

    **Not implemented, deliberately.** This project's rule is not to state
    published figures from memory, and a target curve is exactly the kind of
    thing that gets approximated once and then treated as authoritative for
    years -- every tune afterwards inheriting the error, with nothing in a
    measurement able to reveal it, because the tune will match whatever curve
    it was given.

    To supply the real thing, use :func:`from_points` with values read off the
    source, and record where they came from in the curve's ``name``::

        target = from_points(
            [(20, 6.0), (60, 4.5), ...],
            name="Harman in-car (Olive et al. 2019, fig. N)",
        )

    Or export the curve from software that ships it and load it with
    :func:`from_points`. Either way the provenance travels with the numbers.

    Raises:
        NotImplementedError: Always.
    """
    raise NotImplementedError(
        "No published Harman in-car values are reproduced here -- see the "
        "docstring. Use from_points() with numbers you can cite, or tilted() "
        "for a baseline."
    )


def _band_mean(
    freqs_hz: np.ndarray, values_db: np.ndarray, band_hz: tuple[float, float]
) -> float:
    """Mean of ``values_db`` over a band, weighted evenly per octave.

    A plain mean over a log-spaced axis is already per-octave uniform, but the
    axis is not guaranteed to be log-spaced, so the weighting is made explicit
    rather than assumed. Without it a dense high-frequency axis would let the
    top octave set the level for the whole curve.
    """
    lo, hi = band_hz
    if hi <= lo:
        raise TargetError(f"band {band_hz} is empty or inverted")
    mask = (freqs_hz >= lo) & (freqs_hz <= hi)
    if not np.any(mask):
        raise TargetError(
            f"no frequency points inside {lo:.1f}-{hi:.1f} Hz; the axis spans "
            f"{freqs_hz.min():.1f}-{freqs_hz.max():.1f} Hz"
        )
    if mask.sum() == 1:
        return float(values_db[mask][0])

    log_f = np.log(freqs_hz[mask])
    weights = np.gradient(log_f)
    return float(np.sum(values_db[mask] * weights) / np.sum(weights))


def level_offset_db(
    measured_db: np.ndarray,
    target_db: np.ndarray,
    freqs_hz: np.ndarray,
    band_hz: tuple[float, float] = DEFAULT_LEVEL_BAND_HZ,
) -> float:
    """How far the measurement sits above the target, in dB.

    This is the **channel gain change** the tune needs, and it is deliberately
    kept out of the EQ correction. Letting the fitter absorb a broadband offset
    wastes bands on something a single gain register does exactly.
    """
    measured_db = np.asarray(measured_db, dtype=np.float64)
    target_db = np.asarray(target_db, dtype=np.float64)
    freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
    if not measured_db.shape == target_db.shape == freqs_hz.shape:
        raise TargetError(
            f"measured_db {measured_db.shape}, target_db {target_db.shape} and "
            f"freqs_hz {freqs_hz.shape} must have the same shape"
        )
    return _band_mean(freqs_hz, measured_db - target_db, band_hz)


def correction_db(
    measured_db: np.ndarray,
    target: TargetCurve,
    freqs_hz: np.ndarray,
    band_hz: tuple[float, float] = DEFAULT_LEVEL_BAND_HZ,
) -> tuple[np.ndarray, float]:
    """The curve the EQ should realise, and the gain change to apply separately.

    Returns ``(target_for_fit_db, level_offset_db)``:

    * ``target_for_fit_db`` is the target raised to the measurement's own level,
      so passing it to :func:`tuner.optimize.biquad.fit` alongside
      ``measured_db`` asks the fitter for shape only.
    * ``level_offset_db`` is how much the measurement exceeded the target, which
      is what to subtract from channel gain.

    Splitting them is what keeps a tune inside its band budget, and it is also
    what makes the two adjustments independently checkable afterwards.
    """
    measured_db = np.asarray(measured_db, dtype=np.float64)
    freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
    if measured_db.shape != freqs_hz.shape:
        raise TargetError(
            f"measured_db {measured_db.shape} and freqs_hz {freqs_hz.shape} "
            f"must have the same shape"
        )
    target_db = target.at(freqs_hz)
    offset = level_offset_db(measured_db, target_db, freqs_hz, band_hz)
    return target_db + offset, offset
