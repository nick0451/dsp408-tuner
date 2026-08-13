"""Parametric EQ fitting against a target curve.

The fit is a constrained optimization, not a least-squares inversion of the
measured response. Three constraints matter:

* **Boost is penalized far more heavily than cut.** Filling a null costs
  headroom and amplifier power for little audible benefit -- the null is
  usually a cancellation that moves with listening position anyway.
* **Q is bounded.** Very high-Q corrections fit the measurement beautifully
  and generalize terribly, because they correct a feature specific to the
  exact mic position.
* **Band count is bounded by the hardware.** See ``budget``.

Differential evolution is used rather than gradient descent: the parameter
space is multi-modal and the cost function is cheap to evaluate, which is the
regime DE suits.

Frequency and bandwidth are not treated the same way, and that asymmetry is
measured rather than assumed (2026-08-08, see ``docs/dsp408-protocol.md``):

* **Frequency is effectively continuous** -- 1 Hz steps over a 16-bit field.
  A 450 Hz crossover corner measured 449.4 Hz, so the device honours what it is
  given and the tables read out of the APK are the app's default band layout,
  not a constraint.
* **Bandwidth is genuinely quantized** -- an integer field at 0.01 octave per
  step. So the search runs **in the bandwidth domain, never in Q**: one raw
  step is worth 0.60 in Q near Q = 9.6 and 0.002 near Q = 0.5, a factor of 300.
  Fitting Q continuously and rounding afterwards is a different, worse answer.

The caller still specifies Q bounds, because Q is the engineering unit a person
reasons about. The conversion is exact and happens internally.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace

import numpy as np
from scipy.optimize import differential_evolution, minimize

from ..dsp.backend import Biquad, DeviceLimits, FilterType

#: Below this, a band is doing nothing audible and is not worth the hardware
#: slot it occupies. Pruned after fitting.
NEGLIGIBLE_GAIN_DB = 0.1

#: How much the fitted response may worsen, in dB rms, for a band to be
#: considered unearned and dropped. Well under every repeatability floor this
#: rig has measured (0.003 to 0.059 dB), so a band worth keeping by this test
#: is a band whose effect is at least in principle measurable.
#:
#: A gain threshold alone cannot do this job. The optimizer must place all
#: ``max_bands`` filters -- there is no "off" -- so surplus slots land wherever
#: costs least, which is frequently a pair of 2 dB filters that nearly cancel.
#: Neither is negligible by gain and together they do nothing.
PRUNE_TOLERANCE_DB = 0.05

#: The same test for a band centred **outside the measured frequency axis**,
#: where it is deliberately far stricter. Nothing measured constrains such a
#: band at its own centre -- only the part of its skirt that reaches into the
#: axis -- so it survives only by earning its keep several times over.
#:
#: Not tidiness. Fitting a 450-3500 Hz passband has produced ``+2.1 dB at
#: 7694 Hz`` and ``-8.3 dB at 19 kHz``: invisible to the objective, real
#: filters on the device, and in the boost case real output the stimulus
#: ceiling never accounted for.
#:
#: Refusing the placement outright is the obvious alternative and is **worse**,
#: measured twice: the search must place all ``max_bands`` filters, so surplus
#: slots that used to escape land in band instead, where they cooperate and
#: pruning cannot dissolve them. Out-of-band parking is acting as a
#: pressure-release valve, and the right response is to charge for it rather
#: than to close it.
UNMEASURED_PRUNE_TOLERANCE_DB = 0.5


@dataclass(frozen=True)
class FitConstraints:
    """Bounds and penalties for PEQ fitting."""

    max_bands: int = 10

    #: **Ours, not the device's.** The DSP-408 permits +/-12 dB (measured
    #: 2026-08-12 from the vendor app; ``protocol.DEVICE_EQ_GAIN_DB``). We cut
    #: freely and boost barely, because a boost costs headroom the safety
    #: limiter then has to give back, and because boosting a null usually
    #: amplifies a cancellation that moves with the microphone. Raising this
    #: is a tuning-philosophy decision, not a device-capability one.
    max_boost_db: float = 3.0

    #: Matches the device exactly, which is why it is 12.0 and not a round
    #: number someone liked.
    max_cut_db: float = 12.0

    #: **Also ours.** The device spans Q 0.404 to 28.852 -- see
    #: ``protocol.DEVICE_Q_MIN`` / ``DEVICE_Q_MAX``, both confirmed to three
    #: decimals against the app. We stay well inside: a Q of 28 is a notch
    #: about a twentieth of an octave wide, which at a single microphone
    #: position is more likely to be correcting a measurement artefact than a
    #: real feature, and which rings. Widening these is defensible and should
    #: be a deliberate act with a reason attached.
    min_q: float = 0.5
    max_q: float = 8.0

    boost_penalty_weight: float = 4.0
    freq_range_hz: tuple[float, float] = (20.0, 20_000.0)

    #: Device bandwidth grid. ``None`` searches bandwidth continuously, which
    #: is right for a device whose resolution is fine enough not to matter and
    #: wrong for the DSP-408. Set from the backend's measured encoding rather
    #: than guessed here -- see ``tuner.dsp.protocol.bandwidth_octaves``.
    bandwidth_step_octaves: float | None = None
    bandwidth_min_octaves: float = 0.0

    #: Device frequency grid, same reasoning. ``None`` is continuous.
    freq_step_hz: float | None = None

    #: Device gain grid. 0.1 dB on the DSP-408, which is far below audibility
    #: and below the measurement repeatability floor -- included for the same
    #: reason as the others, so the optimizer never returns a value the
    #: hardware will silently alter.
    gain_step_db: float | None = None

    #: Iteration cap for the search. Lower trades fit quality for wall clock;
    #: with the greedy seed the search is usually converged well before the
    #: scipy default of 1000. Measured figures are in ``docs/STATE.md``.
    max_iterations: int = 300

    #: Differential evolution is stochastic. A fixed seed makes a tune
    #: reproducible, which the provenance rules effectively require: an
    #: unrepeatable optimizer result cannot be compared across sessions.
    seed: int = 0

    def __post_init__(self) -> None:
        if self.max_bands < 1:
            raise ValueError("max_bands must be at least 1")
        if not 0 < self.min_q < self.max_q:
            raise ValueError(f"need 0 < min_q < max_q, got {self.min_q}, {self.max_q}")
        lo, hi = self.freq_range_hz
        if not 0 < lo < hi:
            raise ValueError(f"bad freq_range_hz {self.freq_range_hz}")
        if self.max_boost_db < 0 or self.max_cut_db < 0:
            raise ValueError("boost and cut limits are magnitudes, so non-negative")


#: Conservative starting point. Band count is overridden by the device's
#: actual limit; see ``constraints_for``.
DEFAULT_CONSTRAINTS = FitConstraints()


def constraints_for(
    limits: DeviceLimits, base: FitConstraints = DEFAULT_CONSTRAINTS
) -> FitConstraints:
    """Take the band count from the device rather than from a default.

    The resource budget is a hard constraint, not a post-hoc check, so the
    optimizer should never be handed a band count the hardware cannot hold.
    """
    return replace(base, max_bands=limits.max_peq_per_channel)


# ---------------------------------------------------------------------------
# Bandwidth and Q are the same quantity in two parameterizations. These are
# pure mathematics, not device encodings -- ``tuner.dsp.protocol`` holds the
# DSP-408's raw-integer versions, and a test pins the two against each other.
# ---------------------------------------------------------------------------


def q_from_bandwidth_octaves(octaves: float) -> float:
    """Q of a peaking filter with the given bandwidth in octaves."""
    if octaves <= 0:
        raise ValueError("bandwidth must be positive")
    return 1.0 / (2.0 * math.sinh(math.log(2) / 2.0 * octaves))


def bandwidth_octaves_from_q(q: float) -> float:
    """Bandwidth in octaves of a peaking filter with the given Q."""
    if q <= 0:
        raise ValueError("Q must be positive")
    return math.asinh(1.0 / (2.0 * q)) / (math.log(2) / 2.0)


def _snap(value: float, step: float | None, origin: float = 0.0) -> float:
    if step is None:
        return value
    return origin + round((value - origin) / step) * step


def biquad_coefficients(
    band: Biquad, sample_rate_hz: int
) -> tuple[np.ndarray, np.ndarray]:
    """``(b, a)`` for one band, by the Audio EQ Cookbook (Robert Bristow-Johnson).

    Digital coefficients at the device's own rate, so the frequency warping
    near Nyquist is the real one rather than an analog idealization.
    """
    nyquist = sample_rate_hz / 2.0
    if not 0 < band.freq_hz < nyquist:
        raise ValueError(
            f"band centre {band.freq_hz} Hz must be above 0 and below Nyquist "
            f"({nyquist} Hz)"
        )
    if band.q <= 0:
        raise ValueError(f"Q must be positive, got {band.q}")

    w0 = 2.0 * math.pi * band.freq_hz / sample_rate_hz
    cos_w0 = math.cos(w0)
    alpha = math.sin(w0) / (2.0 * band.q)
    amp = 10.0 ** (band.gain_dbfs / 40.0)

    if band.kind is FilterType.PEAKING:
        b = [1 + alpha * amp, -2 * cos_w0, 1 - alpha * amp]
        a = [1 + alpha / amp, -2 * cos_w0, 1 - alpha / amp]
    elif band.kind is FilterType.LOW_SHELF:
        two_sqrt_a_alpha = 2.0 * math.sqrt(amp) * alpha
        b = [
            amp * ((amp + 1) - (amp - 1) * cos_w0 + two_sqrt_a_alpha),
            2 * amp * ((amp - 1) - (amp + 1) * cos_w0),
            amp * ((amp + 1) - (amp - 1) * cos_w0 - two_sqrt_a_alpha),
        ]
        a = [
            (amp + 1) + (amp - 1) * cos_w0 + two_sqrt_a_alpha,
            -2 * ((amp - 1) + (amp + 1) * cos_w0),
            (amp + 1) + (amp - 1) * cos_w0 - two_sqrt_a_alpha,
        ]
    elif band.kind is FilterType.HIGH_SHELF:
        two_sqrt_a_alpha = 2.0 * math.sqrt(amp) * alpha
        b = [
            amp * ((amp + 1) + (amp - 1) * cos_w0 + two_sqrt_a_alpha),
            -2 * amp * ((amp - 1) + (amp + 1) * cos_w0),
            amp * ((amp + 1) + (amp - 1) * cos_w0 - two_sqrt_a_alpha),
        ]
        a = [
            (amp + 1) - (amp - 1) * cos_w0 + two_sqrt_a_alpha,
            2 * ((amp - 1) - (amp + 1) * cos_w0),
            (amp + 1) - (amp - 1) * cos_w0 - two_sqrt_a_alpha,
        ]
    elif band.kind is FilterType.LOW_PASS:
        b = [(1 - cos_w0) / 2, 1 - cos_w0, (1 - cos_w0) / 2]
        a = [1 + alpha, -2 * cos_w0, 1 - alpha]
    elif band.kind is FilterType.HIGH_PASS:
        b = [(1 + cos_w0) / 2, -(1 + cos_w0), (1 + cos_w0) / 2]
        a = [1 + alpha, -2 * cos_w0, 1 - alpha]
    else:  # pragma: no cover - FilterType is exhaustive
        raise ValueError(f"unsupported filter type {band.kind}")

    a = np.asarray(a, dtype=np.float64)
    return np.asarray(b, dtype=np.float64) / a[0], a / a[0]


def _unit_circle(freqs_hz: np.ndarray, sample_rate_hz: int) -> tuple[np.ndarray, ...]:
    """``exp(-jw)`` and ``exp(-2jw)`` for a frequency axis.

    Hoisted out of the fitting loop: these depend only on the axis and the
    sample rate, and the optimizer evaluates tens of thousands of candidate
    chains against the same axis.
    """
    w = 2.0 * np.pi * np.asarray(freqs_hz, dtype=np.float64) / sample_rate_hz
    z1 = np.exp(-1j * w)
    return z1, z1 * z1


def _response_db_on(
    bands: tuple[Biquad, ...],
    z1: np.ndarray,
    z2: np.ndarray,
    sample_rate_hz: int,
) -> np.ndarray:
    total = np.zeros(z1.size, dtype=np.float64)
    for band in bands:
        if not band.enabled:
            continue
        b, a = biquad_coefficients(band, sample_rate_hz)
        num = b[0] + b[1] * z1 + b[2] * z2
        den = a[0] + a[1] * z1 + a[2] * z2
        total += 20.0 * np.log10(np.abs(num / den) + 1e-30)
    return total


def response_db(
    bands: tuple[Biquad, ...],
    freqs_hz: np.ndarray,
    sample_rate_hz: int,
) -> np.ndarray:
    """Combined magnitude response of a biquad chain, in dB.

    Evaluated at the device's actual sample rate -- biquad response warps near
    Nyquist, and fitting against an idealized analog response produces filters
    that measure differently once loaded.

    Disabled bands contribute nothing, so a chain can be evaluated with slots
    switched off without rebuilding it.
    """
    freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
    if freqs_hz.ndim != 1 or freqs_hz.size == 0:
        raise ValueError("freqs_hz must be a non-empty 1-D array")
    z1, z2 = _unit_circle(freqs_hz, sample_rate_hz)
    return _response_db_on(bands, z1, z2, sample_rate_hz)


def _bands_from_vector(
    x: np.ndarray, constraints: FitConstraints, snap: bool = True
) -> tuple[Biquad, ...]:
    """Decode a DE parameter vector into bands, snapped to the device grid.

    Snapping happens **here**, inside the cost function's view of the world, so
    the optimizer scores the response the hardware will actually produce rather
    than one it cannot reach.
    """
    bands = []
    for i in range(len(x) // 3):
        log_f, gain_db, octaves = x[3 * i : 3 * i + 3]
        freq_hz = 10.0**log_f
        if snap:
            freq_hz = _snap(freq_hz, constraints.freq_step_hz)
            gain_db = _snap(gain_db, constraints.gain_step_db)
            octaves = _snap(
                octaves,
                constraints.bandwidth_step_octaves,
                constraints.bandwidth_min_octaves,
            )
        bands.append(
            Biquad(
                freq_hz=float(freq_hz),
                gain_dbfs=float(gain_db),
                q=q_from_bandwidth_octaves(float(octaves)),
                kind=FilterType.PEAKING,
            )
        )
    return tuple(bands)


def objective(
    bands: tuple[Biquad, ...],
    measured_db: np.ndarray,
    target_db: np.ndarray,
    freqs_hz: np.ndarray,
    sample_rate_hz: int,
    constraints: FitConstraints = DEFAULT_CONSTRAINTS,
) -> float:
    """The quantity :func:`fit` minimizes. Lower is better.

    Public because **it is not the same as the residual error**, and comparing
    two candidate chains by RMS alone rewards the wrong thing. A chain that
    fills a null with +9 dB of boost has a lower RMS and is a worse tune: it
    spends headroom and amplifier power correcting a cancellation that moves
    with the listening position. The boost term is what encodes that judgement,
    so any comparison between fits has to include it.

    Boost is penalized on the correction curve rather than on the band gains,
    because what costs headroom is the summed boost actually applied at a
    frequency, not how many bands conspired to produce it.

    **The shape term is mean-centred; the boost term is not.** A peaking chain
    sits at 0 dB between its filters, so it cannot move the whole curve -- that
    is what channel gain does. Asking the fitter to match an absolute level
    therefore asks for something it can only approximate by boosting, which is
    the one thing it is built to avoid. Centring hands the constant back to
    gain, where it belongs, and makes this cost agree with the quantity the run
    is actually scored on: ``MagnitudeObjective`` re-level-matches before it
    scores, so a constant is invisible to the verdict and was mandatory here.

    Boost stays uncentred because boost is absolute: +3 dB costs headroom
    whatever the rest of the curve is doing.

    Measured 2026-08-12 on M4's known answer -- one exactly-representable band,
    2514 Hz / -12.00 dB / 0.47 oct, which the run had to reproduce. Uncentred
    the fitter scored 0.81 dB rms and split it across two straddling notches;
    centred it returns ``2514.0 Hz, -12.00 dB, 0.47 oct`` and scores 0.008 dB.
    """
    correction = response_db(bands, freqs_hz, sample_rate_hz)
    residual = np.asarray(measured_db) - np.asarray(target_db) + correction
    residual = residual - np.mean(residual)
    boost = np.clip(correction, 0.0, None)
    return float(
        np.mean(residual**2) + constraints.boost_penalty_weight * np.mean(boost**2)
    )


def _greedy_seed(
    error_db: np.ndarray,
    freqs_hz: np.ndarray,
    sample_rate_hz: int,
    constraints: FitConstraints,
) -> np.ndarray:
    """A decent starting chain, placed by peel-off rather than by search.

    Repeatedly: find the worst remaining error, put a band on it, subtract what
    that band does, continue. This is not a good final answer -- it is greedy
    and never revisits a choice -- but it lands in the right basin, and
    differential evolution over 30 dimensions is enormously more effective
    started from one good member than from a purely random population.

    The bandwidth estimate comes from the width of the feature at half its
    height, measured on the log-frequency axis, which is where a constant
    number of bins is a constant number of octaves.
    """
    remaining = -np.asarray(error_db, dtype=np.float64).copy()
    log_f = np.log2(freqs_hz)
    octaves_lo = bandwidth_octaves_from_q(constraints.max_q)
    octaves_hi = bandwidth_octaves_from_q(constraints.min_q)
    lo_hz, hi_hz = constraints.freq_range_hz
    in_range = (freqs_hz >= lo_hz) & (freqs_hz <= hi_hz)

    vector: list[float] = []
    for _ in range(constraints.max_bands):
        masked = np.where(in_range, np.abs(remaining), 0.0)
        i = int(np.argmax(masked))
        peak = remaining[i]
        if abs(peak) < NEGLIGIBLE_GAIN_DB:
            # Nothing worth correcting left; park the slot mid-range so DE has
            # a neutral starting point rather than a duplicate of band 0.
            vector += [math.log10(math.sqrt(lo_hz * hi_hz)), 0.0, octaves_hi]
            continue

        half = abs(peak) / 2.0
        lo = i
        while lo > 0 and abs(remaining[lo]) > half:
            lo -= 1
        hi = i
        while hi < remaining.size - 1 and abs(remaining[hi]) > half:
            hi += 1
        octaves = float(np.clip(log_f[hi] - log_f[lo], octaves_lo, octaves_hi))
        gain = float(np.clip(peak, -constraints.max_cut_db, constraints.max_boost_db))

        vector += [math.log10(float(freqs_hz[i])), gain, octaves]
        band = Biquad(
            freq_hz=float(freqs_hz[i]),
            gain_dbfs=gain,
            q=q_from_bandwidth_octaves(octaves),
            kind=FilterType.PEAKING,
        )
        remaining -= response_db((band,), freqs_hz, sample_rate_hz)

    return np.asarray(vector, dtype=np.float64)


def fit(
    measured_db: np.ndarray,
    target_db: np.ndarray,
    freqs_hz: np.ndarray,
    sample_rate_hz: int,
    constraints: FitConstraints = DEFAULT_CONSTRAINTS,
) -> tuple[Biquad, ...]:
    """Fit a PEQ chain correcting ``measured_db`` toward ``target_db``.

    ``freqs_hz`` is expected to be **log-spaced**, which is also how the error
    gets weighted: a uniform mean over a log axis weights each octave equally,
    which is what matching by ear rewards. A linear axis would let the top
    octave dominate the fit.

    Returns at most ``constraints.max_bands`` bands, and fewer when the extra
    slots earn nothing -- a band below ``NEGLIGIBLE_GAIN_DB`` is dropped rather
    than spent.
    """
    measured_db = np.asarray(measured_db, dtype=np.float64)
    target_db = np.asarray(target_db, dtype=np.float64)
    freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
    if not measured_db.shape == target_db.shape == freqs_hz.shape:
        raise ValueError(
            f"measured_db {measured_db.shape}, target_db {target_db.shape} and "
            f"freqs_hz {freqs_hz.shape} must have the same shape"
        )
    nyquist = sample_rate_hz / 2.0
    if np.any(freqs_hz <= 0) or np.any(freqs_hz >= nyquist):
        raise ValueError(f"freqs_hz must be above 0 and below Nyquist ({nyquist} Hz)")

    lo_hz, hi_hz = constraints.freq_range_hz
    hi_hz = min(hi_hz, nyquist * 0.99)
    # Q bounds are the caller's engineering units; the search runs in octaves,
    # where the device's grid is uniform. High Q is narrow, hence the swap.
    octaves_lo = bandwidth_octaves_from_q(constraints.max_q)
    octaves_hi = bandwidth_octaves_from_q(constraints.min_q)

    bounds = []
    for _ in range(constraints.max_bands):
        bounds.append((math.log10(lo_hz), math.log10(hi_hz)))
        bounds.append((-constraints.max_cut_db, constraints.max_boost_db))
        bounds.append((octaves_lo, octaves_hi))

    error_without_eq = measured_db - target_db
    z1, z2 = _unit_circle(freqs_hz, sample_rate_hz)

    def cost(x: np.ndarray, snap: bool = True) -> float:
        # Identical to objective(), but reusing the precomputed unit circle --
        # this runs tens of thousands of times. A test pins the two together.
        bands = _bands_from_vector(x, constraints, snap=snap)
        correction = _response_db_on(bands, z1, z2, sample_rate_hz)
        residual = error_without_eq + correction
        residual = residual - np.mean(residual)
        boost = np.clip(correction, 0.0, None)
        return float(
            np.mean(residual**2) + constraints.boost_penalty_weight * np.mean(boost**2)
        )

    # Seed the population: one greedy member, the rest random within bounds.
    # A blind population over 3 x max_bands dimensions converges slowly enough
    # to blow the project's stated optimization budget several times over.
    rng = np.random.default_rng(constraints.seed)
    n_params = len(bounds)
    population = rng.uniform(
        low=[b[0] for b in bounds],
        high=[b[1] for b in bounds],
        size=(max(16, 2 * n_params), n_params),
    )
    # Seed against the **centred** error, the same quantity the cost scores.
    # An uncentred seed spends its first bands on a broadband offset that the
    # cost does not charge for and a peaking chain cannot make -- and because
    # the seed decides which basin the search lands in, that choice survives
    # to the answer. Measured 2026-08-12: with an 18 dB offset in the target,
    # a centred cost and an uncentred seed gave two fits differing by 0.81 dB
    # in shape, for inputs that differ only by a constant.
    population[0] = np.clip(
        _greedy_seed(
            error_without_eq - float(np.mean(error_without_eq)),
            freqs_hz,
            sample_rate_hz,
            constraints,
        ),
        [b[0] for b in bounds],
        [b[1] for b in bounds],
    )

    result = differential_evolution(
        cost,
        bounds,
        init=population,
        maxiter=constraints.max_iterations,
        seed=constraints.seed,
        # Polishing inside DE runs a gradient step against the snapped cost,
        # which is a staircase -- it stalls immediately and can return a worse
        # point. Polishing is done below, on the smooth surface, and the result
        # is re-snapped and only kept if it actually wins.
        polish=False,
        tol=1e-4,
    )

    best_x, best_cost = result.x, cost(result.x)
    polished = minimize(
        lambda x: cost(x, snap=False), result.x, bounds=bounds, method="L-BFGS-B"
    )
    if polished.success and cost(polished.x) < best_cost:
        best_x = polished.x

    bands = _bands_from_vector(best_x, constraints)
    bands = tuple(b for b in bands if abs(b.gain_dbfs) >= NEGLIGIBLE_GAIN_DB)

    def chain_cost(chain: tuple[Biquad, ...]) -> float:
        correction = _response_db_on(chain, z1, z2, sample_rate_hz)
        residual = error_without_eq + correction
        residual = residual - np.mean(residual)
        boost = np.clip(correction, 0.0, None)
        return float(
            np.mean(residual**2) + constraints.boost_penalty_weight * np.mean(boost**2)
        )

    lo_measured, hi_measured = float(freqs_hz.min()), float(freqs_hz.max())

    def measured(band: Biquad) -> bool:
        return lo_measured <= band.freq_hz <= hi_measured

    return _prune(bands, chain_cost, measured)


def _prune(
    bands: tuple[Biquad, ...],
    chain_cost: Callable[[tuple[Biquad, ...]], float],
    measured: Callable[[Biquad], bool] | None = None,
    tolerance_db: float = PRUNE_TOLERANCE_DB,
    unmeasured_tolerance_db: float = UNMEASURED_PRUNE_TOLERANCE_DB,
) -> tuple[Biquad, ...]:
    """Drop bands that do not earn the slot they occupy.

    Backward elimination: repeatedly remove whichever remaining band costs
    least to lose, while that cost stays under the tolerance for added rms
    deviation. Stops at the first band that is worth keeping.

    **A band centred outside the measured axis must earn much more**, which is
    what ``measured`` selects and ``unmeasured_tolerance_db`` prices. There is
    no evidence for such a band at its own centre frequency -- only for the
    part of its skirt that reaches into the axis -- so keeping one on the same
    terms as a measured band applies an evidence standard the data cannot
    support. The hazard is concrete: a fit over a 450-3500 Hz passband has
    produced ``+2.1 dB at 7694 Hz``, which on a tweeter is real output the
    stimulus ceiling never accounted for.

    **A gain threshold cannot replace this**, which is why both exist. The
    search must place every one of ``max_bands`` filters -- a Biquad has no
    "off" the optimizer can select -- so surplus slots go wherever costs
    least. In practice that means pairs of one- and two-decibel filters that
    nearly cancel: not negligible individually, worth nothing together, and
    written to the device as real filters.

    Cheap enough to be unconditional: at most ``n(n+1)/2`` chain evaluations
    of a response the fitter has already evaluated tens of thousands of times.
    """
    kept = list(bands)

    def budget(band: Biquad) -> float:
        # A cost budget, not a response budget: the cost is a mean square, so
        # an rms tolerance enters squared.
        if measured is None or measured(band):
            return tolerance_db**2
        return unmeasured_tolerance_db**2

    while kept:
        base = chain_cost(tuple(kept))
        best_i, best_delta = None, None
        for i, band in enumerate(kept):
            delta = chain_cost(tuple(kept[:i] + kept[i + 1 :])) - base
            if delta > budget(band):
                continue
            if best_delta is None or delta < best_delta:
                best_i, best_delta = i, delta
        if best_i is None:
            break
        kept.pop(best_i)
    return tuple(kept)
