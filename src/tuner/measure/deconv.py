"""Deconvolution of swept-sine captures into impulse responses.

A deconvolution bug does not announce itself -- it produces a smooth, plausible
curve that is simply wrong, and every downstream stage inherits the error. This
module is validated by known-answer tests: a synthetic system with an
analytically known response must be recovered exactly. See tests/test_measure.py.

**Index convention.** ``deconvolve`` returns the full linear convolution of the
capture with the inverse filter. Zero delay lands at ``Sweep.t_zero_index``
(equivalently ``len(inverse) - 1``), *not* at index 0 -- the time-reversed
inverse necessarily introduces that offset. A system with delay *d* puts its
peak at ``t_zero_index + d``. Harmonic distortion products appear *before*
t_zero and are discarded by windowing, not here.
"""

from __future__ import annotations

import numpy as np
from scipy.fft import irfft, next_fast_len, rfft


def deconvolve(capture: np.ndarray, inverse: np.ndarray) -> np.ndarray:
    """Convolve ``capture`` with the sweep's ``inverse`` filter via FFT.

    Returns the impulse response as float64. See the module docstring for
    where zero delay lands in the returned array.

    Args:
        capture: Recorded signal, 1-D. Longer than the stimulus by whatever
            capture tail was used; the extra length preserves the decay.
        inverse: The matched inverse filter from the same
            :class:`~tuner.measure.sweep.Sweep` that produced the stimulus.
    """
    capture = np.asarray(capture, dtype=np.float64)
    inverse = np.asarray(inverse, dtype=np.float64)

    if capture.ndim != 1 or inverse.ndim != 1:
        raise ValueError("deconvolve operates on 1-D signals; loop over channels")
    if capture.size == 0 or inverse.size == 0:
        raise ValueError("capture and inverse must be non-empty")

    n = capture.size + inverse.size - 1
    nfft = next_fast_len(n)
    return irfft(rfft(capture, nfft) * rfft(inverse, nfft), nfft)[:n]


def peak_index(impulse: np.ndarray) -> int:
    """Index of the largest-magnitude sample.

    Uses absolute value: a polarity-inverted channel still has its arrival
    found, and inversion is a separate finding rather than a missed peak.
    """
    return int(np.argmax(np.abs(impulse)))


def arrival_offset_samples(
    impulse: np.ndarray,
    reference: np.ndarray,
) -> int:
    """Acoustic arrival offset relative to the loopback ``reference``.

    Both arrays must be deconvolution results from the *same* capture session
    -- the reference being the hardware loopback channel, the impulse being a
    microphone channel.

    Only meaningful when a hardware loopback was captured. Without one the
    absolute arrival time is unknown and every derived delay carries an
    unknown constant offset; see the timing-reference rule in CLAUDE.md.
    :class:`~tuner.measure.result.Measurement` enforces that rule -- this
    function is the arithmetic behind it and does not police its own inputs.

    Returns:
        Offset in samples. Positive means the impulse arrives after the
        reference, which is the normal case for an acoustic path.
    """
    return peak_index(impulse) - peak_index(reference)


def harmonic_window(
    impulse: np.ndarray,
    t_zero: int,
) -> np.ndarray:
    """The portion of a deconvolution result preceding the linear impulse.

    In the Farina method harmonic distortion products deconvolve to arrivals
    *before* t_zero. This slice is normally discarded, but it is where
    distortion is measured from -- the same capture yields both.
    """
    if t_zero < 0:
        raise ValueError("t_zero must be non-negative")
    return impulse[:t_zero]
