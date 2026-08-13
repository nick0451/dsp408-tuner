"""Microphone calibration file I/O (REW-compatible format).

REW ``.cal`` files are plain text: comment lines beginning with ``*`` (some
tools use ``#``), then whitespace-separated
``frequency_hz sensitivity_db [phase_deg]`` rows.

Note the format stores phase in **degrees**. Everything internal to this
project uses radians -- conversion happens here, at the boundary, and nowhere
else.

Calibration files are committed to the repository; raw captures are not.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_COMMENT_PREFIXES = ("*", "#", '"')


@dataclass(frozen=True)
class CalibrationCurve:
    """A microphone's frequency-dependent correction.

    Attributes:
        freqs_hz: Frequency points, ascending.
        sensitivity_db: Correction to **add** to measured magnitude.
        phase_rad: Phase correction in radians, if the file provided one.
        source: Path the curve was loaded from.
        sha256: Hash of the file contents, recorded in measurement provenance
            so an edited cal file is detected rather than silently changing
            results.
    """

    freqs_hz: np.ndarray
    sensitivity_db: np.ndarray
    phase_rad: np.ndarray | None = None
    source: Path | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.freqs_hz.shape != self.sensitivity_db.shape:
            raise ValueError("freqs_hz and sensitivity_db must be the same length")
        if self.freqs_hz.size == 0:
            raise ValueError("calibration curve is empty")
        if np.any(np.diff(self.freqs_hz) <= 0):
            raise ValueError("freqs_hz must be strictly ascending")
        if self.phase_rad is not None and self.phase_rad.shape != self.freqs_hz.shape:
            raise ValueError("phase_rad must match freqs_hz in length")

    def at(self, freqs_hz: np.ndarray) -> np.ndarray:
        """Interpolate the sensitivity correction onto ``freqs_hz``.

        Interpolation is linear in dB against **log** frequency, which is how
        the curve is actually shaped; interpolating against linear frequency
        would distort the densely-sampled low end.

        Requests outside the file's range are **clamped** to the endpoint
        values, not extrapolated. Extrapolating a calibration curve invents
        correction where none was measured, and it does so most aggressively
        at the frequency extremes where mics are least well behaved.
        """
        freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
        if np.any(freqs_hz <= 0):
            raise ValueError("frequencies must be positive")
        return np.interp(
            np.log10(freqs_hz),
            np.log10(self.freqs_hz),
            self.sensitivity_db,
            left=float(self.sensitivity_db[0]),
            right=float(self.sensitivity_db[-1]),
        )

    def apply(self, magnitude_db: np.ndarray, freqs_hz: np.ndarray) -> np.ndarray:
        """Correct a measured magnitude response with this curve."""
        return np.asarray(magnitude_db, dtype=np.float64) + self.at(freqs_hz)

    @property
    def range_hz(self) -> tuple[float, float]:
        """Frequency range the curve actually covers."""
        return float(self.freqs_hz[0]), float(self.freqs_hz[-1])


def load(path: Path) -> CalibrationCurve:
    """Read a REW-format ``.cal`` file.

    Phase, if present, is converted from the file's degrees to radians.
    """
    path = Path(path)
    freqs: list[float] = []
    sens: list[float] = []
    phases: list[float] = []

    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith(_COMMENT_PREFIXES):
            continue
        parts = line.replace(",", " ").split()
        if len(parts) < 2:
            raise ValueError(f"{path}:{lineno}: expected at least 2 columns: {raw!r}")
        try:
            freqs.append(float(parts[0]))
            sens.append(float(parts[1]))
            if len(parts) >= 3:
                phases.append(float(parts[2]))
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: unparseable row: {raw!r}") from exc

    if not freqs:
        raise ValueError(f"{path}: no data rows found")
    if phases and len(phases) != len(freqs):
        raise ValueError(f"{path}: phase column present on only some rows")

    return CalibrationCurve(
        freqs_hz=np.array(freqs, dtype=np.float64),
        sensitivity_db=np.array(sens, dtype=np.float64),
        phase_rad=np.deg2rad(phases) if phases else None,
        source=path,
        sha256=file_sha256(path),
    )


def save(curve: CalibrationCurve, path: Path, comment: str = "") -> None:
    """Write a REW-format ``.cal`` file.

    Phase is written in **degrees** per the format, converted from the
    radians used internally.
    """
    path = Path(path)
    lines = [f"* {line}" for line in (comment or "Generated by tuner").splitlines()]
    lines.append(
        "* Freq(Hz) SPL(dB) Phase(deg)"
        if curve.phase_rad is not None
        else "* Freq(Hz) SPL(dB)"
    )

    if curve.phase_rad is None:
        lines.extend(
            f"{f:.4f} {s:.4f}"
            for f, s in zip(curve.freqs_hz, curve.sensitivity_db, strict=True)
        )
    else:
        lines.extend(
            f"{f:.4f} {s:.4f} {p:.4f}"
            for f, s, p in zip(
                curve.freqs_hz,
                curve.sensitivity_db,
                np.rad2deg(curve.phase_rad),
                strict=True,
            )
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    """Hash a calibration file's contents for provenance tracking."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
