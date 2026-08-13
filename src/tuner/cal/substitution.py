"""Substitution (comparison) microphone calibration.

Derives a calibration curve for an uncalibrated microphone by comparing it
against a reference mic in an identical sound field:

    Cal_DUT(f) = Cal_REF(f) + [Mag_REF(f) - Mag_DUT(f)]

This is what commercial calibration services do; they differ only in using a
laboratory reference instead of a UMIK-1. Stacked error lands around
+/-1.5-2 dB absolute -- worse than a factory calibration, vastly better than
none.

What matters more here than absolute accuracy is **relative matching**. Mics
calibrated against the same reference by the same procedure share their
common-mode error, so the residual mismatch between array elements is small.
Spatial averaging and L/R asymmetry -- the things a car array exists for --
depend on that relative match, not on absolute accuracy.

Two constraints govern whether the result is trustworthy:

* **Capsule position, not body position, must match between the two
  measurements.** Mic bodies differ in diameter. This needs a swap fixture.
* **Orientation must be fixed at 0 degrees.** Calibration curves are
  angle-dependent above roughly 8 kHz.

Status: Milestone 2. Not yet implemented.
"""

from __future__ import annotations

import numpy as np

from ..measure.result import Measurement

#: Below this, gated free-field data is invalid and coupler data is used.
DEFAULT_SPLICE_HZ = 250.0


def derive_calibration(
    reference: Measurement,
    device_under_test: Measurement,
    reference_cal_db: np.ndarray,
    freqs_hz: np.ndarray,
) -> np.ndarray:
    """Calibration curve for the DUT from a same-field comparison.

    Both measurements must share provenance apart from the microphone itself.
    """
    raise NotImplementedError("Milestone 2")


def splice(
    low_freq_curve_db: np.ndarray,
    high_freq_curve_db: np.ndarray,
    freqs_hz: np.ndarray,
    splice_hz: float = DEFAULT_SPLICE_HZ,
) -> np.ndarray:
    """Blend coupler-derived LF data into gated free-field HF data.

    Crossfades over a range around ``splice_hz`` rather than switching
    abruptly, and applies the offset that aligns the two curves in the overlap
    region before blending.
    """
    raise NotImplementedError("Milestone 2")
