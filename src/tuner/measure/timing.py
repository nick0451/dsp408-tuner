"""An acoustic timing reference, and the clock correction it makes possible.

A hardware loopback establishes t=0 by wiring an interface output back to an
interface input, so the reference and the measurement share both a clock and a
known electrical path. A USB measurement microphone can do neither: it is on
its own crystal, and there is no cable to loop.

The alternative is to put the clapperboard **in the air**::

    generated (interface clock)
        REF_A ---- gap ---- measurement sweep ---- gap ---- REF_B
                   |<------ known interval, exactly ------>|

    captured (microphone clock)
        detect A                                        detect B
                   |<---- interval as the mic heard it -->|

One loudspeaker -- any stable one, typically a tweeter -- plays a short chirp
before and after the sweep. Both are detected in the capture by matched
filter. Two independent things fall out, and they are worth keeping separate:

**A common time origin.** Every measurement in a session is referred to the
same acoustic event through the same reference speaker, so their arrivals are
comparable *to each other* even for a driver that could never reproduce the
chirp. A subwoofer's arrival becomes measurable because the reference, not the
subwoofer, carries the timing.

**A clock-rate estimate.** The interval between the two detections, divided by
the interval that was generated, is the ratio of the two clocks over exactly
the window that matters. Correcting the capture's timebase by it removes the
skew that would otherwise smear the impulse and tilt the phase.

What this is **not** is a loopback, and the difference is not cosmetic.

* The reference speaker's own propagation delay is an unknown constant. It
  cancels between measurements and never resolves, so **relative timing is
  recovered and absolute timing is not.** :class:`TimingReference` carries
  that distinction into the type system rather than a comment.
* The constant only cancels while the microphone and the reference speaker
  stay put. Move either and previously comparable measurements silently stop
  being so -- which is what ``Provenance.setup_token`` exists to declare.
* The reference output's own DSP delay and crossover are inside the constant.
  A run that writes a delay to the reference channel moves t=0 for every
  measurement after it, by exactly that amount, and the result looks like an
  acoustic change rather than an error.

Measured on this rig 2026-08-13: the interface output and the UMIK-1 agree to
better than 167 ppm, which is all a frame-count comparison can resolve at a
480-sample buffer. That bound is not good enough to ignore -- 167 ppm over a
2 s sweep is 16 samples, which is inaudible in magnitude and **3.3 cycles at
10 kHz** in phase. The two-chirp interval is the only instrument here that
measures the composite ratio, over the real window, including any resampling
the host does out of sight.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
from scipy.fft import next_fast_len, rfft
from scipy.signal import resample

#: Default reference chirp band, matching REW's acoustic timing reference.
#: Deliberately **not** the sweep's band -- the reference is a timing event,
#: not a measurement, and what it is optimised for is a sharp arrival.
#:
#: Wide beats low, because the correlation peak's width is set by the
#: bandwidth. 5-20 kHz resolves arrivals ~200 us apart (69 mm); the 2-8 kHz
#: band this used to default to resolved only ~500 us (172 mm), which is
#: coarse next to the delays a car tune actually applies.
#:
#: Two consequences follow and neither is optional. **The reference output
#: must have a tweeter** -- a subwoofer cannot carry this signal, which is
#: exactly why the timing reference and the alignment reference are different
#: drivers chosen by different criteria. And it puts 5-20 kHz into a tweeter
#: twice per measurement, so hard safety rule 4 applies with full force: the
#: reference output's ceiling should be the most conservative in the run.
REFERENCE_START_HZ = 5_000.0
REFERENCE_STOP_HZ = 20_000.0

#: Short. It is fired twice per measurement, every measurement, into whatever
#: driver was nominated -- so it is the most frequently emitted signal in a
#: whole tuning run and the one whose energy budget matters most.
REFERENCE_DURATION_S = 0.05

#: Fraction of the matched filter's peak that a candidate must exceed to count
#: as an arrival. The **first** such peak is taken, not the largest: in a
#: cabin the direct sound is followed within a millisecond by a windshield
#: reflection, and a reflection that arrives louder than the direct path is
#: entirely possible off-axis. Taking the maximum would then report the
#: reflection's arrival as t=0 -- consistently, and with no sign of trouble.
ARRIVAL_THRESHOLD = 0.5


class TimingReference(Enum):
    """What established t=0 for a capture, and therefore what may be reported.

    Three states rather than a boolean, because an acoustic reference sits
    between the other two and collapsing it into either one is a real error.

    ``arrival_samples`` on a loopback capture means two things at once -- the
    arrival's index into the impulse, *and* the propagation delay -- and the
    fact that they are one number is what stops them drifting apart. Under an
    acoustic reference the second meaning becomes "propagation delay minus the
    reference path", so the identity breaks. Reporting an acoustic capture as
    though it had a loopback would let absolute-delay figures out under a flag
    designed to prevent exactly that.
    """

    #: No reference. Magnitude, RT60 and spatial averaging only.
    NONE = "none"

    #: An interface output wired back to an interface input. One clock, known
    #: path, absolute delay valid.
    LOOPBACK = "loopback"

    #: A reference loudspeaker heard by the measurement microphone. Relative
    #: delay and phase valid **between measurements sharing the geometry**;
    #: absolute delay unknown by an unmeasured constant.
    ACOUSTIC = "acoustic"

    @property
    def gives_absolute_delay(self) -> bool:
        return self is TimingReference.LOOPBACK

    @property
    def gives_relative_delay(self) -> bool:
        return self is not TimingReference.NONE


@dataclass(frozen=True)
class ReferenceSignal:
    """The chirp used as an acoustic clapperboard."""

    samples: np.ndarray
    sample_rate_hz: int
    start_hz: float
    stop_hz: float

    @property
    def n(self) -> int:
        return int(self.samples.size)


def reference_chirp(
    sample_rate_hz: int,
    duration_s: float = REFERENCE_DURATION_S,
    start_hz: float = REFERENCE_START_HZ,
    stop_hz: float = REFERENCE_STOP_HZ,
) -> ReferenceSignal:
    """A short band-limited sweep, windowed so its matched filter is sharp.

    A linear sweep rather than a log one: the reference is not measuring
    anything, so equal energy per hertz is what is wanted, and a flat spectrum
    across the band gives the narrowest correlation peak.

    The Hann window matters more than it looks. An unwindowed chirp starts and
    stops abruptly, and those discontinuities put energy across the whole
    spectrum -- which shows up as sidelobes on the correlation, one of which
    can beat the true peak once a reflection is added to it.
    """
    if duration_s <= 0:
        raise ValueError("reference duration must be positive")
    nyquist = sample_rate_hz / 2.0
    if not 0 < start_hz < stop_hz < nyquist:
        raise ValueError(
            f"reference band {start_hz}-{stop_hz} Hz must be inside "
            f"0-{nyquist} Hz and ascending"
        )
    n = int(round(duration_s * sample_rate_hz))
    t = np.arange(n, dtype=np.float64) / sample_rate_hz
    rate = (stop_hz - start_hz) / duration_s
    phase = 2.0 * np.pi * (start_hz * t + 0.5 * rate * t * t)
    samples = np.sin(phase) * np.hanning(n)
    peak = float(np.max(np.abs(samples)))
    if peak > 0:
        samples = samples / peak
    return ReferenceSignal(samples, sample_rate_hz, start_hz, stop_hz)


@dataclass(frozen=True)
class Arrival:
    """Where a reference chirp was detected, and how confidently."""

    #: Sub-sample index into the capture. Fractional because a whole sample at
    #: 48 kHz is 21 microseconds, which is 7 mm of path -- coarse enough to
    #: matter for a clock ratio measured over a few seconds.
    index: float

    #: Matched-filter peak height at that index, normalised to the largest
    #: peak in the search window.
    strength: float

    #: True when a later, stronger peak exists -- i.e. this arrival was taken
    #: as the *first* above threshold rather than the largest. Expected
    #: indoors, and worth surfacing rather than hiding: a direct arrival much
    #: weaker than a reflection means the microphone is pointing the wrong way
    #: or the reference speaker is obstructed.
    weaker_than_a_later_peak: bool


def _matched_filter(captured: np.ndarray, reference: ReferenceSignal) -> np.ndarray:
    """Cross-correlation of the capture with the reference, via FFT."""
    x = np.asarray(captured, dtype=np.float64).ravel()
    h = np.asarray(reference.samples, dtype=np.float64).ravel()
    n = next_fast_len(x.size + h.size)
    # Correlation is convolution with the time-reversed kernel; done in the
    # real-FFT domain because a reference chirp is thousands of samples and
    # this runs twice per measurement.
    from scipy.fft import irfft

    corr = irfft(rfft(x, n) * np.conj(rfft(h, n)), n)
    return corr[: x.size]


def _refine(values: np.ndarray, i: int) -> float:
    """Parabolic interpolation around a discrete peak."""
    if i <= 0 or i >= values.size - 1:
        return float(i)
    a, b, c = values[i - 1], values[i], values[i + 1]
    denom = a - 2.0 * b + c
    if denom == 0:
        return float(i)
    return float(i) + 0.5 * (a - c) / denom


def detect_arrival(
    captured: np.ndarray,
    reference: ReferenceSignal,
    search: slice | None = None,
    threshold: float = ARRIVAL_THRESHOLD,
) -> Arrival:
    """Find the reference chirp in ``captured``. First peak, not the loudest.

    Raises:
        ValueError: If the search window holds no correlation energy at all,
            which means the reference was not played, not heard, or looked for
            in the wrong place. Returning an arbitrary index there would put a
            plausible number on a measurement that never happened.
    """
    x = np.asarray(captured, dtype=np.float64).ravel()
    corr = np.abs(_matched_filter(x, reference))
    lo = 0 if search is None or search.start is None else int(search.start)
    hi = x.size if search is None or search.stop is None else int(search.stop)
    lo, hi = max(0, lo), min(x.size, hi)
    if hi - lo < 3:
        raise ValueError(f"search window [{lo}, {hi}) is too small to hold a peak")

    window = corr[lo:hi]
    largest = float(np.max(window))
    if largest <= 0.0:
        raise ValueError(
            "no reference arrival: the matched filter found no energy at all "
            "in the search window. The reference was not played, was not "
            "heard, or was looked for in the wrong part of the capture."
        )

    above = np.flatnonzero(window >= threshold * largest)
    if above.size == 0:  # pragma: no cover - largest is itself above threshold
        above = np.array([int(np.argmax(window))])

    # Take the strongest point in the **first** group, not the first point
    # above threshold. A matched filter's main lobe is not a single spike:
    # its width is set by the reference's bandwidth, and inside that width it
    # ripples. On this chirp the ripples put five local maxima above half the
    # peak, spread over 18 samples, so a naive "first local maximum" reads 9
    # samples early -- 190 us, or 65 mm of apparent path, every time.
    #
    # The guard is three over the bandwidth, which is a little wider than the
    # lobe. It is also a real limit and not a tuning knob: **two arrivals
    # closer together than the inverse bandwidth cannot be separated at all**,
    # by this or any other method. A 6 kHz-wide reference resolves arrivals
    # about 170 us apart; anything closer is one arrival as far as it is
    # concerned.
    bandwidth_hz = reference.stop_hz - reference.start_hz
    guard = int(round(3.0 * reference.sample_rate_hz / bandwidth_hz))
    first_group = window[above[0] : min(above[0] + guard + 1, window.size)]
    best = above[0] + int(np.argmax(first_group))

    # Compared by index, not by height. "Is the peak we took smaller than the
    # largest one" is true by a float hair when they are the same peak, and a
    # flag that is set on every clean measurement is a flag nobody reads.
    return Arrival(
        index=_refine(window, best) + lo,
        strength=float(window[best] / largest),
        weaker_than_a_later_peak=bool(int(np.argmax(window)) > best + guard),
    )


@dataclass(frozen=True)
class ClockRatio:
    """How fast the capture clock ran relative to the playback clock."""

    #: captured interval / generated interval. Greater than 1 means the
    #: capture device counted more samples than the playback device did over
    #: the same real time, i.e. its clock is fast.
    ratio: float
    generated_interval_samples: int
    captured_interval_samples: float

    @property
    def ppm(self) -> float:
        return (self.ratio - 1.0) * 1e6

    def skew_samples(self, over_samples: int) -> float:
        """Accumulated skew across a window, in samples. What the fuss is about."""
        return abs(self.ratio - 1.0) * over_samples


#: Beyond this, the two devices are not plausibly two free-running crystals --
#: a detection landed on the wrong peak, or the intervals do not correspond.
#: Consumer audio crystals are specified in tens of ppm; a whole percent is a
#: different failure wearing a plausible number.
MAX_PLAUSIBLE_PPM = 10_000.0


def estimate_clock_ratio(
    first: Arrival,
    second: Arrival,
    generated_interval_samples: int,
) -> ClockRatio:
    """Ratio of the two clocks, from the interval between the two chirps.

    ``generated_interval_samples`` is the distance between the two references
    **as generated**, which is known exactly because we built the buffer.

    Raises:
        ValueError: If the implied ratio is not physically plausible. The
            failure this catches is a detection that landed on a reflection
            or on the wrong chirp, and its symptom is a correction that
            stretches the capture into nonsense while looking like arithmetic.
    """
    if generated_interval_samples <= 0:
        raise ValueError("the generated interval must be positive")
    captured = float(second.index - first.index)
    if captured <= 0:
        raise ValueError(
            f"the second reference was detected at or before the first "
            f"({second.index:.1f} <= {first.index:.1f}); the two detections "
            f"are not the two chirps"
        )
    ratio = captured / generated_interval_samples
    result = ClockRatio(ratio, generated_interval_samples, captured)
    if abs(result.ppm) > MAX_PLAUSIBLE_PPM:
        raise ValueError(
            f"implied clock ratio {ratio:.6f} ({result.ppm:+.0f} ppm) is not "
            f"two free-running crystals. A detection has landed on the wrong "
            f"peak, or the generated interval does not describe this capture."
        )
    return result


def correct_timebase(captured: np.ndarray, ratio: ClockRatio) -> np.ndarray:
    """Resample a capture onto the playback device's timebase.

    Band-limited (FFT) resampling rather than interpolation. The correction is
    a fraction of a sample per thousand, so a linear interpolator's error
    would be small -- but it is a *frequency-dependent* small, worst exactly
    where the skew already does its damage, and there is no reason to add one
    error while removing another.
    """
    x = np.asarray(captured, dtype=np.float64)
    target = int(round(x.shape[0] / ratio.ratio))
    if target < 1:
        raise ValueError("clock correction produced an empty capture")
    if target == x.shape[0]:
        return x
    return np.asarray(resample(x, target, axis=0), dtype=np.float64)
