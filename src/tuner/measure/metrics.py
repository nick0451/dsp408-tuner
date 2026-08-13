"""Derived acoustic metrics: frequency response, RT60, spatial averaging.

Frequency axes are log-spaced throughout, and every function states its point
count and range explicitly rather than accepting a bare array.

The frequency response is evaluated **exactly** at the requested frequencies
via ``scipy.signal.freqz``, treating the impulse response as an FIR numerator.
This avoids interpolating from FFT bins onto a log axis, which would smear the
low-frequency end -- precisely where a log axis is densest and FFT bins are
sparsest.
"""

from __future__ import annotations

import numpy as np
from scipy import signal
from scipy.fft import next_fast_len, rfft, rfftfreq

from .result import Measurement

DEFAULT_FREQ_RANGE_HZ = (20.0, 20_000.0)
DEFAULT_FREQ_POINTS = 512

#: Floor applied before taking a logarithm, so silence yields a large negative
#: number rather than -inf or a warning.
_EPS = 1e-30


def log_freqs(
    start_hz: float = DEFAULT_FREQ_RANGE_HZ[0],
    stop_hz: float = DEFAULT_FREQ_RANGE_HZ[1],
    points: int = DEFAULT_FREQ_POINTS,
) -> np.ndarray:
    """Log-spaced frequency axis."""
    if start_hz <= 0 or stop_hz <= 0:
        raise ValueError("frequencies must be positive")
    if stop_hz <= start_hz:
        raise ValueError("stop_hz must exceed start_hz")
    if points < 2:
        raise ValueError("need at least 2 points")
    return np.logspace(np.log10(start_hz), np.log10(stop_hz), points)


def _checked_freqs(freqs_hz: np.ndarray, sample_rate_hz: int) -> np.ndarray:
    """Validate a frequency axis against the sample rate."""
    freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
    nyquist = sample_rate_hz / 2.0
    if np.any(freqs_hz <= 0):
        raise ValueError("frequencies must be positive")
    if np.any(freqs_hz >= nyquist):
        raise ValueError(
            f"frequencies must be below Nyquist ({nyquist} Hz); "
            f"highest requested was {freqs_hz.max()} Hz"
        )
    return freqs_hz


def frequency_response(
    impulse: np.ndarray,
    sample_rate_hz: int,
    freqs_hz: np.ndarray,
) -> np.ndarray:
    """Complex transfer function at exactly ``freqs_hz``.

    Frequencies at or above Nyquist are rejected rather than silently aliased.
    """
    freqs_hz = _checked_freqs(freqs_hz, sample_rate_hz)
    _, h = signal.freqz(impulse, [1.0], worN=freqs_hz, fs=sample_rate_hz)
    return h


def magnitude_db(
    impulse: np.ndarray,
    sample_rate_hz: int,
    freqs_hz: np.ndarray,
) -> np.ndarray:
    """Magnitude response in dB at ``freqs_hz``."""
    h = frequency_response(impulse, sample_rate_hz, freqs_hz)
    return 20.0 * np.log10(np.abs(h) + _EPS)


#: Zero-padding factor for phase unwrapping. Bin spacing must be fine enough
#: that phase advances by less than pi between bins; 4x gives 2x margin for an
#: impulse whose group delay runs to its full length.
_PHASE_PAD_FACTOR = 4


def phase_rad(
    impulse: np.ndarray,
    sample_rate_hz: int,
    freqs_hz: np.ndarray,
    unwrap: bool = True,
) -> np.ndarray:
    """Phase response in radians at ``freqs_hz``.

    Radians internally; convert at display boundaries only.

    **Unwrapping is done on a dense linear axis and then interpolated**, never
    on the caller's axis. ``np.unwrap`` assumes adjacent points differ by less
    than pi, which a log-spaced axis violates at its sparse high end -- it
    fails silently there, returning a smooth curve that is simply wrong. Since
    every frequency axis in this project is log-spaced, doing it the direct way
    would be wrong everywhere it matters.
    """
    freqs_hz = _checked_freqs(freqs_hz, sample_rate_hz)

    if not unwrap:
        return np.angle(frequency_response(impulse, sample_rate_hz, freqs_hz))

    impulse = np.asarray(impulse, dtype=np.float64)
    nfft = next_fast_len(_PHASE_PAD_FACTOR * impulse.size)
    dense_hz = rfftfreq(nfft, 1.0 / sample_rate_hz)
    dense_phase = np.unwrap(np.angle(rfft(impulse, nfft)))
    return np.interp(freqs_hz, dense_hz, dense_phase)


def group_delay_samples(
    impulse: np.ndarray,
    sample_rate_hz: int,
    freqs_hz: np.ndarray,
) -> np.ndarray:
    """Group delay in samples at ``freqs_hz``.

    Derived from the unwrapped phase, so it inherits the dense-axis treatment
    above. Delay is in samples internally; convert at display boundaries.

    .. warning::
       **This takes a bare impulse array and therefore cannot enforce the
       timing-reference rule.** Prefer
       :meth:`tuner.measure.result.Measurement.group_delay_samples`, which
       raises when no hardware loopback was captured. Calling this directly on
       a measurement that lacks one yields a curve offset by an unknown
       constant -- plausible-looking and wrong, which is the failure mode the
       rule exists to prevent.
    """
    freqs_hz = _checked_freqs(freqs_hz, sample_rate_hz)
    phase = phase_rad(impulse, sample_rate_hz, freqs_hz)
    omega = 2.0 * np.pi * freqs_hz / sample_rate_hz
    return -np.gradient(phase, omega)


def octave_bands(
    start_hz: float = 31.25,
    stop_hz: float = 16_000.0,
    fraction: int = 1,
) -> np.ndarray:
    """Centre frequencies of fractional-octave bands, base-2 from 1 kHz.

    Centres are exact powers of two relative to 1 kHz (31.25, 62.5, 125 ...),
    not the rounded ISO nominal names (31.5, 63, 125 ...). The default
    ``start_hz`` is 31.25 so the lowest band is actually included -- passing
    the nominal 31.5 would exclude it.
    """
    if fraction < 1:
        raise ValueError("fraction must be >= 1")
    lo = np.ceil(fraction * np.log2(start_hz / 1000.0))
    hi = np.floor(fraction * np.log2(stop_hz / 1000.0))
    return 1000.0 * 2.0 ** (np.arange(lo, hi + 1) / fraction)


def _bandpass(
    impulse: np.ndarray,
    sample_rate_hz: int,
    centre_hz: float,
    fraction: int = 1,
) -> np.ndarray:
    """Zero-phase fractional-octave bandpass.

    ``filtfilt`` is used so the filter contributes no group delay of its own,
    which would otherwise bias the decay slope.
    """
    factor = 2.0 ** (1.0 / (2.0 * fraction))
    low = centre_hz / factor
    high = min(centre_hz * factor, sample_rate_hz / 2.0 * 0.999)
    if low >= high:
        raise ValueError(f"band at {centre_hz} Hz does not fit below Nyquist")
    sos = signal.butter(
        4, [low, high], btype="bandpass", fs=sample_rate_hz, output="sos"
    )
    return signal.sosfiltfilt(sos, impulse)


def schroeder_decay_db(impulse: np.ndarray) -> np.ndarray:
    """Backward-integrated energy decay curve, normalized to 0 dB at t=0."""
    energy = np.cumsum(impulse[::-1] ** 2)[::-1]
    if energy[0] <= 0:
        return np.full(impulse.shape, -np.inf)
    return 10.0 * np.log10(energy / energy[0] + _EPS)


def _decay_time(
    decay_db: np.ndarray,
    sample_rate_hz: int,
    upper_db: float,
    lower_db: float,
) -> float:
    """Fit the decay slope between two levels and extrapolate to 60 dB.

    Returns NaN when the decay never reaches ``lower_db`` -- an honest "not
    measurable from this capture" rather than a number extrapolated from noise.
    """
    below_upper = np.flatnonzero(decay_db <= upper_db)
    below_lower = np.flatnonzero(decay_db <= lower_db)
    if below_upper.size == 0 or below_lower.size == 0:
        return float("nan")

    start, stop = int(below_upper[0]), int(below_lower[0])
    if stop - start < 2:
        return float("nan")

    samples = np.arange(start, stop, dtype=np.float64)
    slope_db_per_sample, _ = np.polyfit(samples, decay_db[start:stop], 1)
    if slope_db_per_sample >= 0:
        return float("nan")

    return float(-60.0 / slope_db_per_sample / sample_rate_hz)


def rt60_from_impulse(
    impulse: np.ndarray,
    sample_rate_hz: int,
    bands_hz: np.ndarray | None = None,
    method: str = "t20",
    fraction: int = 1,
) -> tuple[np.ndarray, np.ndarray]:
    """Reverberation time per band via Schroeder backward integration.

    Valid without a timing reference -- RT60 is a decay rate, so it does not
    depend on knowing absolute arrival time.

    ``method`` selects the fit range: ``"t20"`` fits -5 to -25 dB, ``"t30"``
    fits -5 to -35 dB. T30 is more robust when the noise floor allows it; T20
    works on shorter or noisier decays.

    Returns:
        ``(bands_hz, rt60_seconds)``. Bands whose decay never reaches the fit
        range yield NaN rather than a fabricated figure.
    """
    ranges = {"t20": (-5.0, -25.0), "t30": (-5.0, -35.0)}
    if method not in ranges:
        raise ValueError(f"method must be one of {sorted(ranges)}; got {method!r}")
    upper_db, lower_db = ranges[method]

    if bands_hz is None:
        bands_hz = octave_bands(stop_hz=min(16_000.0, sample_rate_hz / 2.0 / 1.5))
    bands_hz = np.asarray(bands_hz, dtype=np.float64)

    times = np.empty(bands_hz.size, dtype=np.float64)
    for i, centre in enumerate(bands_hz):
        band = _bandpass(impulse, sample_rate_hz, float(centre), fraction=fraction)
        times[i] = _decay_time(
            schroeder_decay_db(band), sample_rate_hz, upper_db, lower_db
        )
    return bands_hz, times


def rt60(
    measurement: Measurement,
    bands_hz: np.ndarray | None = None,
    method: str = "t20",
) -> tuple[np.ndarray, np.ndarray]:
    """Reverberation time of a :class:`Measurement`. See ``rt60_from_impulse``."""
    return rt60_from_impulse(
        measurement.impulse,
        measurement.provenance.sample_rate_hz,
        bands_hz=bands_hz,
        method=method,
    )


def spatial_average(
    measurements: list[Measurement],
    freqs_hz: np.ndarray,
) -> np.ndarray:
    """Average magnitude across listening positions, in dB.

    Averaging reduces the influence of position-specific modal nulls -- a deep
    null at one microphone position is often a cancellation that moves a few
    centimetres away, and correcting it wastes headroom on a problem that is
    not general.

    Two modes, which give **different answers**:

    * **Complex averaging**, when every input has a valid timing reference.
      Phase relationships are preserved, so nulls common across positions
      survive and position-specific ones cancel.
    * **Power (RMS) averaging**, when none do. Cannot distinguish a real null
      from a phase artifact, and systematically reads higher at nulls.

    Mixing the two within one call is rejected rather than silently coerced --
    the two modes are not comparable and quietly picking one would make the
    result depend on capture history.
    """
    if not measurements:
        raise ValueError("need at least one measurement to average")

    rates = {m.provenance.sample_rate_hz for m in measurements}
    if len(rates) != 1:
        raise ValueError(f"measurements have differing sample rates: {sorted(rates)}")
    sample_rate_hz = rates.pop()

    referenced = [m.has_timing_reference for m in measurements]
    if any(referenced) and not all(referenced):
        raise ValueError(
            "cannot average measurements with and without a timing reference "
            "in one call: complex and power averaging give different answers, "
            "and silently choosing one would make the result depend on which "
            "captures happened to have a loopback"
        )

    responses = np.array(
        [frequency_response(m.impulse, sample_rate_hz, freqs_hz) for m in measurements]
    )

    if all(referenced):
        averaged = responses.mean(axis=0)
    else:
        averaged = np.sqrt((np.abs(responses) ** 2).mean(axis=0))

    return 20.0 * np.log10(np.abs(averaged) + _EPS)
