"""Log-swept sine stimulus generation (Farina method).

A logarithmic sine sweep plus its matched inverse filter lets deconvolution
separate the linear impulse response from harmonic distortion products, which
appear as separate arrivals *before* the linear one in the deconvolved result.
That separation is why this method is used instead of MLS or an impulse.

Generated stimuli are unity-peak and otherwise unscaled -- levelling and
limiting happen in ``tuner.safety``, which is the only sanctioned path to an
output device.

**The stimulus and its inverse are generated together and travel together** as
a :class:`Sweep`. Deconvolving with a mismatched inverse does not error; it
produces a smooth, plausible, wrong answer. Pairing them in one object removes
that failure mode by construction.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal
from scipy.fft import irfft, next_fast_len, rfft


@dataclass(frozen=True)
class Sweep:
    """A log sine sweep and the inverse filter that deconvolves it.

    Attributes:
        samples: Unity-peak stimulus, float64.
        inverse: Matched inverse filter, normalized so that deconvolving
            ``samples`` with it yields a unit-amplitude impulse.
        start_hz: Sweep start frequency.
        stop_hz: Sweep stop frequency.
        duration_s: Sweep length, excluding any capture tail.
        sample_rate_hz: Rate both arrays were generated at.
    """

    samples: np.ndarray
    inverse: np.ndarray
    start_hz: float
    stop_hz: float
    duration_s: float
    sample_rate_hz: int

    @property
    def t_zero_index(self) -> int:
        """Index in a deconvolution result corresponding to zero delay.

        Convolving the stimulus with the (time-reversed) inverse places the
        linear impulse at this index. A system with delay *d* puts its peak at
        ``t_zero_index + d``.
        """
        return len(self.inverse) - 1


def _raised_cosine_fade(n_samples: int, fade_len: int, at_start: bool) -> np.ndarray:
    """Half a Hann window, for tapering a stimulus edge."""
    env = np.ones(n_samples)
    if fade_len <= 0:
        return env
    fade_len = min(fade_len, n_samples)
    ramp = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_len) / fade_len))
    if at_start:
        env[:fade_len] = ramp
    else:
        env[n_samples - fade_len :] = ramp[::-1]
    return env


def log_sweep(
    start_hz: float,
    stop_hz: float,
    duration_s: float,
    sample_rate_hz: int,
    fade_in_ms: float = 10.0,
    fade_out_ms: float = 10.0,
) -> Sweep:
    """Generate a unity-peak logarithmic sine sweep and its inverse filter.

    The instantaneous frequency rises exponentially from ``start_hz`` to
    ``stop_hz``. Raised-cosine fades at both ends suppress the broadband click
    an abrupt start or stop would produce.

    The inverse filter is the time-reversed sweep with an amplitude envelope
    rising 6 dB/octave with frequency, compensating the log sweep's
    -3 dB/octave magnitude tilt so the deconvolved result is flat. It is then
    normalized numerically against the actual (faded) stimulus, so the fades
    do not bias the result.

    **Sweep wider than the band you care about.** Response is flat to within
    ~0.02 dB across the interior, but ripples within roughly an octave of each
    endpoint and rolls off outside them. To measure 20 Hz-20 kHz honestly,
    sweep 10 Hz-22 kHz (or as close as Nyquist allows) rather than exactly
    20 Hz-20 kHz.
    """
    if start_hz <= 0 or stop_hz <= 0:
        raise ValueError("sweep frequencies must be positive")
    if stop_hz <= start_hz:
        raise ValueError("stop_hz must exceed start_hz")
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    if stop_hz > sample_rate_hz / 2:
        raise ValueError(
            f"stop_hz {stop_hz} exceeds Nyquist {sample_rate_hz / 2} "
            f"for sample rate {sample_rate_hz}"
        )

    n = int(round(duration_s * sample_rate_hz))
    if n < 2:
        raise ValueError("sweep is shorter than two samples")

    t = np.arange(n, dtype=np.float64) / sample_rate_hz
    rate = np.log(stop_hz / start_hz)

    # Farina: phase(t) = (w1*T/R) * (exp(t*R/T) - 1)
    phase = (2.0 * np.pi * start_hz * duration_s / rate) * (
        np.expm1(t * rate / duration_s)
    )
    samples = np.sin(phase)

    samples *= _raised_cosine_fade(
        n, int(round(fade_in_ms * sample_rate_hz / 1000.0)), at_start=True
    )
    samples *= _raised_cosine_fade(
        n, int(round(fade_out_ms * sample_rate_hz / 1000.0)), at_start=False
    )

    # Time-reverse, then apply the +6 dB/octave envelope. In the reversed
    # signal the instantaneous frequency falls from stop_hz to start_hz, so an
    # amplitude proportional to frequency is a decaying exponential.
    envelope = np.exp(-np.arange(n, dtype=np.float64) * rate / n)
    inverse = samples[::-1] * envelope

    inverse /= _passband_gain(samples, inverse, sample_rate_hz, start_hz, stop_hz)

    return Sweep(
        samples=samples,
        inverse=inverse,
        start_hz=float(start_hz),
        stop_hz=float(stop_hz),
        duration_s=float(duration_s),
        sample_rate_hz=int(sample_rate_hz),
    )


def _passband_gain(
    samples: np.ndarray,
    inverse: np.ndarray,
    sample_rate_hz: int,
    start_hz: float,
    stop_hz: float,
) -> float:
    """Mean in-band magnitude of the stimulus deconvolved with its own inverse.

    Used to normalize the inverse so a perfect system measures 0 dB.

    Note this deliberately does **not** normalize the time-domain peak to 1.
    The deconvolved impulse is band-limited, so its peak is
    ``2(f2-f1)/fs`` times its passband gain -- normalizing the peak would leave
    every magnitude measurement offset by that ratio (about 1.6 dB for a
    20 Hz-20 kHz sweep at 48 kHz). What must be flat is the response, not the
    peak sample.

    The gain is averaged over the band *interior* (an octave inside each edge),
    because the sweep's endpoints are where its energy distribution is least
    well behaved.
    """
    n = len(samples) + len(inverse) - 1
    nfft = next_fast_len(n)
    reference = irfft(rfft(samples, nfft) * rfft(inverse, nfft), nfft)[:n]

    interior = np.geomspace(
        min(start_hz * 2.0, sample_rate_hz / 2.0 * 0.4),
        min(stop_hz / 2.0, sample_rate_hz / 2.0 * 0.9),
        128,
    )
    _, response = signal.freqz(reference, [1.0], worN=interior, fs=sample_rate_hz)

    gain = float(np.mean(np.abs(response)))
    if gain == 0.0:
        raise ValueError("degenerate sweep: inverse filter has no in-band response")
    return gain
