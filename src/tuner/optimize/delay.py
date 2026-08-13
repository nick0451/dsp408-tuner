"""Per-driver time alignment.

**Every function here requires measurements with a valid timing reference.**
Without one, arrival times carry an unknown constant offset and the resulting
alignment is arbitrary. That is enforced rather than documented: the accessors
these functions call raise instead of returning a plausible number.

The multi-position case has no exact solution -- drivers cannot arrive
simultaneously at two different seats. The solver optimises a weighted
compromise, with the weighting an explicit input rather than a hidden default.

Why arrival times, and not cross-correlation between drivers
------------------------------------------------------------
This module's original sketch called for cross-correlating each driver against
the reference. **That is the wrong instrument here, and it fails hardest on the
pair that matters most.**

Cross-correlation measures the lag that best aligns two signals, which is only
meaningful where both have energy. A tweeter crossed at 3.5 kHz and a
mid-woofer rolled off at 450 Hz share essentially no passband, so their
correlation peak is formed from stopband leakage and noise -- it is broad,
sensitive to the noise floor, and can land an octave's worth of samples away
while looking perfectly confident.

Arrival time avoids the problem entirely. With a hardware loopback,
``Measurement.arrival_samples`` **is** the propagation delay -- one number
serving as both the index into the impulse and the time of flight, which is
what keeps the two from drifting apart. Each driver's arrival is measured
against the loopback, never against another driver, so no shared bandwidth is
required and every figure is independently valid.

The residual caveat, recorded rather than solved: a band-limited driver's
impulse peak sits slightly later than its true acoustic arrival, by roughly the
group delay of its own crossover. That bias is systematic per driver and small
next to the inter-driver differences being corrected, but it is a bias, and it
is the reason a tune is verified acoustically rather than trusted from
arithmetic.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from ..measure.result import Measurement


class AlignmentError(ValueError):
    """Raised when an alignment problem is malformed or unsolvable."""


def relative_delay_samples(
    driver: Measurement,
    reference: Measurement,
) -> int:
    """Arrival-time difference between two drivers, in samples.

    Positive means ``driver`` arrives **later** than ``reference``, so it is
    the closer speaker that needs delay added, not this one.

    Raises:
        NoTimingReference: If either measurement lacks a hardware loopback.
            Both are checked, and the check is the accessor's, not a copy of
            it here.
    """
    return int(driver.delay_samples() - reference.delay_samples())


def align(
    drivers: dict[int, list[Measurement]],
    position_weights: np.ndarray | Sequence[float] | None = None,
) -> dict[int, int]:
    """Per-output delays in samples that best align arrivals.

    ``drivers`` maps output index to that driver's measurement at each
    listening position, in a consistent position order.
    ``position_weights`` sets the compromise between positions; uniform if
    omitted. Weights need not sum to one.

    Returned delays are non-negative integers, normalised so the minimum is
    zero. Only relative delay matters acoustically, and on this device delay
    RAM is a shared pool, so an unnecessary common offset is spent capacity --
    often the difference between a tune that loads and one that does not.

    The solution
    ------------
    With one position the answer is exact: delay every driver until it matches
    the latest one.

    With several it is a least-squares compromise. Minimising the weighted sum
    over positions of the spread of arrival times gives a closed form -- no
    iteration, no starting guess. Writing ``a[i][j]`` for driver ``i``'s
    arrival at position ``j``, and subtracting each position's mean to remove
    the part no delay can influence, each driver's ideal shift is the weighted
    mean of its own deviations::

        c[i] = weighted_mean_j( a[i][j] - mean_k a[k][j] )
        d[i] = max(c) - c[i]

    A driver that is consistently early across seats gets the most delay; one
    that is early at some seats and late at others gets a compromise, which is
    the honest answer rather than a good one. **No amount of delay aligns a
    driver at two seats at once**, and the residual spread this leaves is worth
    reporting to the operator rather than hiding.

    Raises:
        AlignmentError: If the inputs are inconsistent or empty.
        NoTimingReference: If any measurement lacks a hardware loopback.
    """
    if not drivers:
        raise AlignmentError("no drivers given")

    outputs = sorted(drivers)
    counts = {len(drivers[o]) for o in outputs}
    if len(counts) != 1:
        raise AlignmentError(
            f"every driver needs a measurement at the same positions; got "
            f"{ {o: len(drivers[o]) for o in outputs} }"
        )
    (n_positions,) = counts
    if n_positions == 0:
        raise AlignmentError("drivers have no measurements")

    weights = _weights(position_weights, n_positions)

    # delay_samples() raises without a timing reference, which is where the
    # rule is enforced -- not by a duplicate check here.
    arrivals = np.array(
        [[m.delay_samples() for m in drivers[o]] for o in outputs],
        dtype=np.float64,
    )

    # Remove each position's mean: a constant added to every driver at one
    # position is common propagation, and no choice of delay can change it.
    deviations = arrivals - arrivals.mean(axis=0, keepdims=True)
    ideal = deviations @ weights / weights.sum()

    delays = np.rint(ideal.max() - ideal).astype(int)
    delays = _refine_integers(delays, arrivals, weights)
    delays = delays - delays.min()
    return dict(zip(outputs, (int(d) for d in delays), strict=True))


def _weighted_spread_cost(
    delays: np.ndarray, arrivals: np.ndarray, weights: np.ndarray
) -> float:
    """Weighted sum over positions of the squared arrival spread."""
    shifted = arrivals + delays[:, None]
    centred = shifted - shifted.mean(axis=0, keepdims=True)
    return float((weights * (centred**2).sum(axis=0)).sum())


def _refine_integers(
    delays: np.ndarray, arrivals: np.ndarray, weights: np.ndarray
) -> np.ndarray:
    """Coordinate descent over +/-1, to recover what rounding gave away.

    The closed form is exact over the reals, but the device stores integer
    samples and rounding each driver independently ignores that the drivers
    are coupled -- every one of them appears in every position's mean. The
    loss is tiny (0.008% on a three-driver, two-seat case, far below a
    sample) and acoustically irrelevant, but it is cheap to remove and it
    lets this function's docstring say *minimises* without an asterisk.

    Each component starts within half a sample of its real optimum, so single
    steps suffice; the loop is bounded regardless.
    """
    best = np.array(delays, dtype=int)
    best_cost = _weighted_spread_cost(best, arrivals, weights)

    for _ in range(len(best) * 4):
        improved = False
        for i in range(len(best)):
            for step in (-1, 1):
                trial = best.copy()
                trial[i] += step
                cost = _weighted_spread_cost(trial, arrivals, weights)
                if cost < best_cost - 1e-9:
                    best, best_cost, improved = trial, cost, True
        if not improved:
            break
    return best


def residual_spread_samples(
    drivers: dict[int, list[Measurement]],
    delays: dict[int, int],
) -> list[float]:
    """Arrival spread remaining at each position after applying ``delays``.

    Zero at a position means every driver arrives together there. With more
    than one position some spread is unavoidable, and this is the number that
    says how much the compromise cost -- **report it rather than presenting a
    multi-seat alignment as if it were exact.**

    At 44.1 kHz one sample is 7.8 mm of path length, so a spread of a few
    samples is below what moving your head undoes.
    """
    outputs = sorted(drivers)
    missing = [o for o in outputs if o not in delays]
    if missing:
        raise AlignmentError(f"no delay given for output(s) {missing}")

    arrivals = np.array(
        [[m.delay_samples() + delays[o] for m in drivers[o]] for o in outputs],
        dtype=np.float64,
    )
    return [float(col.max() - col.min()) for col in arrivals.T]


def _weights(
    position_weights: np.ndarray | Sequence[float] | None, n_positions: int
) -> np.ndarray:
    if position_weights is None:
        return np.ones(n_positions, dtype=np.float64)
    weights = np.asarray(position_weights, dtype=np.float64)
    if weights.shape != (n_positions,):
        raise AlignmentError(
            f"position_weights has {weights.shape} entries, expected {n_positions}"
        )
    if np.any(weights < 0):
        raise AlignmentError("position weights must not be negative")
    if weights.sum() <= 0:
        raise AlignmentError("position weights sum to zero")
    return weights
