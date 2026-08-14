"""Read a measurement exported from Room EQ Wizard.

REW is already this project's independent reference -- ``tests/test_golden_rew.py``
compares our engine against it, and on 2026-08-13 five REW sweeps settled a
diagnosis our own instruments could not. This module makes that ingestion a
supported path rather than something re-written per analysis.

**Why a second measurement source is worth having.** REW measures things we do
not: harmonic distortion, and dB SPL against a calibrated microphone. Both are
real gaps -- the bench session that prompted this module stalled on audible
distortion that our level-linearity check could only see indirectly, through
the noise floor of a room. Using REW where REW is stronger costs nothing and
is not the same as replacing an engine that already agrees with it to 0.094 dB
rms and repeats 4.6x better.

**What it deliberately does not do.** Reading a REW export does not make a
:class:`~tuner.measure.result.Measurement`. A measurement carries provenance,
a timing reference and a coupling, and REW's file carries none of those in a
form this project can check. The caller builds that, declares it, and takes
responsibility for it -- which is the same rule that applies to a measurement
we took ourselves.

**And REW's stimulus does not pass through** :mod:`tuner.safety`. That is the
central caveat of the whole hybrid: hard safety rule 1 has no jurisdiction
over a sweep another program plays. On a bench with the operator present that
is their call. In a car with tweeters connected it is not a detail.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class RewFileError(ValueError):
    """Raised when a file is not a REW measurement export we can read."""


#: Header fields worth carrying into provenance, mapped from REW's own labels.
#: REW writes them as ``* Label: value`` before the data.
_HEADER_FIELDS = {
    "Measurement": "title",
    "Source": "source",
    "Format": "stimulus",
    "Dated": "measured_at",
    "Smoothing": "smoothing",
}


@dataclass(frozen=True)
class RewMeasurement:
    """One REW measurement export.

    Attributes:
        freqs_hz: Frequency points, ascending.
        magnitude_dbspl: REW's ``SPL(dB)`` column.

            **Only true dB SPL if the operator calibrated it.** REW always
            labels the column SPL; without an SPL calibration it carries an
            arbitrary offset. That is harmless for everything this project
            does with it, because ``MagnitudeObjective`` level-matches before
            it scores and a fit solves for shape -- but an
            *absolute* level read off this field is a claim the file cannot
            support. Named ``_dbspl`` because the domain is acoustic; the
            caveat is about accuracy, not about which domain it is in.
        phase_deg: REW's phase column in **degrees**, as written. Converted to
            radians by :attr:`phase_rad`, never stored that way, so the
            project's radians-internally rule is enforced at the boundary
            rather than hoped for.
        smoothing: REW's own description, e.g. ``None`` or ``1/3 octave``.
            Load-bearing: fitting a smoothed curve under-corrects narrow
            features, and the danger is not that it is wrong but that it is
            invisible.
        sha256: Hash of the file, so an edited export is detected rather than
            silently changing a tune.
    """

    freqs_hz: np.ndarray
    magnitude_dbspl: np.ndarray
    phase_deg: np.ndarray | None = None
    title: str = ""
    source: str = ""
    stimulus: str = ""
    measured_at: str = ""
    smoothing: str = ""
    path: Path | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.freqs_hz.shape != self.magnitude_dbspl.shape:
            raise RewFileError("frequency and magnitude columns differ in length")
        if self.freqs_hz.size < 2:
            raise RewFileError("a measurement needs at least two points")
        if np.any(np.diff(self.freqs_hz) <= 0):
            raise RewFileError("frequencies must be strictly ascending")
        if self.phase_deg is not None and self.phase_deg.shape != self.freqs_hz.shape:
            raise RewFileError("phase column does not match the frequency column")

    @property
    def phase_rad(self) -> np.ndarray | None:
        """Phase in radians. Degrees live only in the file and the display."""
        if self.phase_deg is None:
            return None
        return np.deg2rad(self.phase_deg)

    @property
    def range_hz(self) -> tuple[float, float]:
        return float(self.freqs_hz[0]), float(self.freqs_hz[-1])

    @property
    def is_smoothed(self) -> bool:
        return self.smoothing.strip().lower() not in ("", "none")

    def at(self, freqs_hz: np.ndarray) -> np.ndarray:
        """Interpolate the magnitude onto ``freqs_hz``.

        Linear in dB against **log** frequency, matching
        :meth:`tuner.cal.calfile.CalibrationCurve.at` -- a response is shaped
        against log frequency, and interpolating against linear frequency
        distorts the densely sampled low end.

        Requests outside the file's range are **clamped**, not extrapolated.
        REW exports typically start below 1 Hz and run past 20 kHz, so this
        should never bind in practice; it exists so that when it does, the
        result is a flat continuation rather than an invented slope.
        """
        wanted = np.asarray(freqs_hz, dtype=np.float64)
        if np.any(wanted <= 0):
            raise ValueError("frequencies must be positive")
        return np.interp(
            np.log10(wanted),
            np.log10(self.freqs_hz),
            self.magnitude_dbspl,
            left=float(self.magnitude_dbspl[0]),
            right=float(self.magnitude_dbspl[-1]),
        )

    def summary(self) -> str:
        lo, hi = self.range_hz
        lines = [
            f"  title      {self.title or '(unnamed)'}",
            f"  taken      {self.measured_at or '(undated)'}",
            f"  stimulus   {self.stimulus or '(unrecorded)'}",
            f"  source     {self.source or '(unrecorded)'}",
            f"  range      {lo:.2f} - {hi:.1f} Hz, {self.freqs_hz.size} points",
            f"  smoothing  {self.smoothing or '(unrecorded)'}",
        ]
        if self.sha256:
            lines.append(f"  sha256     {self.sha256[:16]}...")
        return "\n".join(lines)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def load(path: Path) -> RewMeasurement:
    """Parse a REW ``Export measurement as text`` file.

    The format is a run of ``*``-prefixed header lines followed by two or
    three whitespace-separated numeric columns. Blank lines and a trailing
    ``*`` separator are tolerated.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")

    header: dict[str, str] = {}
    freqs: list[float] = []
    magnitude: list[float] = []
    phase: list[float] = []
    saw_phase = False

    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("*"):
            match = re.match(r"\*\s*([A-Za-z ]+?):\s*(.*)$", stripped)
            if match:
                label, value = match.group(1).strip(), match.group(2).strip()
                if label in _HEADER_FIELDS:
                    header[_HEADER_FIELDS[label]] = value
            continue
        parts = stripped.split()
        if len(parts) < 2:
            raise RewFileError(
                f"{path.name} line {number}: expected at least two columns, "
                f"got {stripped!r}"
            )
        # The shape check sits outside the numeric conversion below. Inside
        # it, `RewFileError` -- a ValueError subclass -- would be caught by
        # that except clause and reported as "not numeric", which is a wrong
        # diagnosis of a real problem.
        if len(parts) < 3 and saw_phase:
            raise RewFileError(
                f"{path.name} line {number}: phase column disappears partway "
                f"through the file. A short column silently misaligned "
                f"against the frequencies would be worse than refusing."
            )
        try:
            freqs.append(float(parts[0]))
            magnitude.append(float(parts[1]))
            if len(parts) >= 3:
                phase.append(float(parts[2]))
                saw_phase = True
        except ValueError as exc:
            raise RewFileError(
                f"{path.name} line {number}: not numeric -- {stripped!r}"
            ) from exc

    if not freqs:
        raise RewFileError(
            f"{path.name} contains no data rows. A REW export is header lines "
            f"beginning '*' followed by numeric columns; this file has the "
            f"first and not the second."
        )

    return RewMeasurement(
        freqs_hz=np.asarray(freqs, dtype=np.float64),
        magnitude_dbspl=np.asarray(magnitude, dtype=np.float64),
        phase_deg=np.asarray(phase, dtype=np.float64) if saw_phase else None,
        path=path,
        sha256=_sha256(path),
        **header,
    )


def spread(measurements: list[RewMeasurement], freqs_hz: np.ndarray) -> np.ndarray:
    """Per-frequency spread across repeated measurements, in dB.

    The repeatability of the rig as REW sees it. Kept here rather than in a
    script because it is the number that decides whether a position is worth
    measuring from at all: on 2026-08-13 it was 3.90 dB median at the original
    microphone position and 1.53 dB near-field, which is the difference
    between a usable bench and a wasted afternoon.
    """
    if len(measurements) < 2:
        raise ValueError("spread needs at least two measurements")
    stacked = np.stack([m.at(freqs_hz) for m in measurements])
    return stacked.max(axis=0) - stacked.min(axis=0)
