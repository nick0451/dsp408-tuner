"""Impulse response windowing.

Gating rejects room and cabin reflections to approximate a free-field
measurement, at the cost of low-frequency resolution: a window of length T
gives valid data only above roughly 1/T. A 5 ms gate is therefore trustworthy
above ~200 Hz and meaningless below it.

In a car the first reflection typically arrives within a few milliseconds, so
a window long enough to reach 100 Hz already contains reflections. **There is
no gate setting that yields clean free-field data at low frequencies inside a
vehicle.** That is a physical limit, not a tuning problem, and it is why the
calibration rig uses a sealed coupler for low frequencies instead -- see
``tuner.cal.coupler``.
"""

from __future__ import annotations

import numpy as np

#: Fraction of the window tapered at the trailing edge.
DEFAULT_TAPER_FRACTION = 0.25


def gate(
    impulse: np.ndarray,
    sample_rate_hz: int,
    window_ms: float,
    arrival_samples: int = 0,
    taper_fraction: float = DEFAULT_TAPER_FRACTION,
) -> np.ndarray:
    """Window ``impulse`` to ``window_ms`` starting at the arrival.

    A half-Hann taper is applied to the trailing edge; rectangular truncation
    produces spectral ripple that is easily mistaken for a real response
    feature. **No taper is applied at the onset** -- the arrival is the signal,
    and fading it in would attenuate exactly the high-frequency content the
    gate exists to preserve.

    Windows extending past the end of the data are zero-padded, so the
    returned length is always the requested window.

    Args:
        impulse: Deconvolved impulse response.
        sample_rate_hz: Rate the impulse was captured at.
        window_ms: Gate length in milliseconds.
        arrival_samples: Index of the acoustic arrival. Typically
            ``deconv.peak_index(impulse)`` or a t_zero-relative offset.
        taper_fraction: Portion of the window to taper, 0 to 1.
    """
    if window_ms <= 0:
        raise ValueError("window_ms must be positive")
    if not 0.0 <= taper_fraction <= 1.0:
        raise ValueError("taper_fraction must be between 0 and 1")
    if arrival_samples < 0:
        raise ValueError("arrival_samples must be non-negative")

    n = int(round(window_ms * sample_rate_hz / 1000.0))
    if n < 1:
        raise ValueError(
            f"window of {window_ms} ms is under one sample at {sample_rate_hz} Hz"
        )

    segment = np.zeros(n, dtype=np.float64)
    available = impulse[arrival_samples : arrival_samples + n]
    segment[: available.size] = available

    taper_len = int(round(n * taper_fraction))
    if taper_len > 1:
        ramp = 0.5 * (1.0 + np.cos(np.pi * np.arange(taper_len) / (taper_len - 1)))
        segment[n - taper_len :] *= ramp

    return segment


def valid_above_hz(window_ms: float) -> float:
    """Lowest frequency for which a ``window_ms`` gate gives valid data."""
    if window_ms <= 0:
        raise ValueError("window_ms must be positive")
    return 1000.0 / window_ms


def window_for_lowest_hz(lowest_hz: float) -> float:
    """Gate length in milliseconds needed to reach ``lowest_hz``.

    The inverse of :func:`valid_above_hz`. Useful for answering "can I gate
    this at all?" -- if the returned window is longer than the time to the
    first reflection, the answer is no.
    """
    if lowest_hz <= 0:
        raise ValueError("lowest_hz must be positive")
    return 1000.0 / lowest_hz
