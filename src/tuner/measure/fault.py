"""Deliberate faults, injected into the stimulus so the tuner must find them.

An "it improved" test cannot tell a good correction from a mediocre one. This
project has been caught by that twice -- a closed loop that reported `accepted`
while landing a decibel from an answer it could express exactly, and a
regression test that shipped green over a 6.3 dB error. The cure both times
was a **known answer**: plant something, demand it back.

This is that, for the acoustic path. The generated sweep is filtered by a
known chain and *then* emitted; the deconvolution still runs against the
**unfiltered** sweep, so the measured response is ``H_fault x H_system`` and
the fault is simply part of the system as far as everything downstream is
concerned. The tuner has to discover it by measurement.

Injecting here rather than upstream matters, for three reasons:

* **The tuner cannot model it away.** A fault written into the DSP as an EQ
  band would be subtracted at fit time by ``TuneRun._without_existing_eq``,
  which reads the channel's live EQ and removes its modelled response. The
  tune would then be correct by arithmetic rather than by hearing anything.
* **It survives exclusive mode.** A system-wide equaliser on Windows (an APO)
  lives in the shared audio engine, and this project opens its output in
  **WASAPI exclusive** mode to reach 48 kHz alongside a UMIK-1. An APO would
  silently not be applied, the tuner would correct nothing, and the result
  would look entirely reasonable.
* **It stays behind the safety limiter.** The fault shapes the stimulus and
  ``safety.apply`` then normalises to the requested level, so a fault with
  +12 dB in it cannot raise what actually leaves the interface.

.. warning::
   **This deliberately corrupts a measurement.** Every capture taken with one
   records its fingerprint in provenance, and provenance refuses to compare a
   faulted measurement with a clean one -- or with a differently-faulted one.
   A bench run is internally consistent and cannot be mistaken for a real one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class FaultFilter:
    """A known filter applied to the stimulus before it is emitted.

    Attributes:
        sos: Second-order sections, shaped ``(n, 6)``, as scipy uses them.
        sample_rate_hz: The rate the coefficients were designed at. Biquad
            response warps near Nyquist, so a chain designed at 48 kHz is a
            different filter at 44.1 kHz -- :meth:`apply_to` refuses the
            mismatch rather than quietly emitting the wrong fault.
        label: What this fault represents, in words. Required, and recorded in
            provenance: a measurement carrying an unexplained corruption is
            worse than no measurement.
    """

    sos: np.ndarray
    sample_rate_hz: int
    label: str

    def __post_init__(self) -> None:
        sos = np.asarray(self.sos, dtype=np.float64)
        if sos.ndim != 2 or sos.shape[1] != 6 or sos.shape[0] < 1:
            raise ValueError(f"sos must be shaped (n, 6) with n >= 1, got {sos.shape}")
        if not self.label or not self.label.strip():
            raise ValueError(
                "a fault filter needs a label saying what it represents. It "
                "goes into provenance, and a corrupted measurement whose "
                "corruption is unexplained is worse than no measurement"
            )
        object.__setattr__(self, "sos", sos)
        object.__setattr__(self, "label", self.label.strip())

    @classmethod
    def from_peaking(
        cls,
        bands: list[tuple[float, float, float]],
        sample_rate_hz: int,
        label: str,
    ) -> FaultFilter:
        """Build from ``(freq_hz, gain_db, q)`` peaking sections.

        The RBJ coefficients come from :mod:`tuner.optimize.biquad`, imported
        late so this module does not depend on the optimizer. One home for
        that arithmetic is the point -- a fault designed by a second
        implementation of the same formulas would be a fault nobody could
        check the fit against.
        """
        from ..dsp.backend import Biquad, FilterType
        from ..optimize.biquad import biquad_coefficients

        rows = []
        for freq_hz, gain_db, q in bands:
            b, a = biquad_coefficients(
                Biquad(
                    freq_hz=freq_hz,
                    gain_dbfs=gain_db,
                    q=q,
                    kind=FilterType.PEAKING,
                ),
                sample_rate_hz,
            )
            rows.append([b[0], b[1], b[2], a[0], a[1], a[2]])
        return cls(np.asarray(rows, dtype=np.float64), sample_rate_hz, label)

    def apply_to(self, samples: np.ndarray, sample_rate_hz: int) -> np.ndarray:
        """Filter a stimulus. Raises on a sample-rate mismatch."""
        if sample_rate_hz != self.sample_rate_hz:
            raise ValueError(
                f"fault {self.label!r} was designed at {self.sample_rate_hz} Hz "
                f"and is being applied at {sample_rate_hz} Hz. Biquad response "
                f"warps near Nyquist, so this would emit a different fault "
                f"than the one the known answer describes."
            )
        from scipy.signal import sosfilt

        return np.asarray(
            sosfilt(self.sos, np.asarray(samples, dtype=np.float64)),
            dtype=np.float64,
        )

    def response_db(self, freqs_hz: np.ndarray) -> np.ndarray:
        """The fault's magnitude response -- the answer the tuner must find.

        Its negative is what a perfect correction would write, which is what
        makes a bench run scoreable against zero instead of against "better".
        """
        from scipy.signal import sosfreqz

        _, h = sosfreqz(
            self.sos,
            worN=2.0
            * np.pi
            * np.asarray(freqs_hz, dtype=np.float64)
            / self.sample_rate_hz,
        )
        return 20.0 * np.log10(np.abs(h) + 1e-30)

    def fingerprint(self) -> str:
        """Identity for provenance: the label **and** the coefficients.

        Both, because a label that stayed the same while the coefficients
        moved is exactly the comparison that must not be allowed to pass.
        """
        digest = hashlib.sha256(
            self.label.encode("utf-8")
            + b"|"
            + f"{self.sample_rate_hz}".encode()
            + b"|"
            + np.ascontiguousarray(self.sos).tobytes()
        ).hexdigest()
        return f"{self.label} [{digest[:12]}]"
