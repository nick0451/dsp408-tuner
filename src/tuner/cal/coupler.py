"""Sealed-coupler low-frequency calibration.

Gating cannot be used below roughly 200 Hz -- the window needed to reject
reflections is longer than the period being measured. The coupler solves this
differently: a small airtight cavity with a driver and two grommeted microphone
ports. Below the cavity's first resonant mode the pressure is uniform
throughout, so both capsules see an identical stimulus regardless of position,
giving clean comparison data from a few Hz upward.

Coupler results are spliced into gated free-field results around 250 Hz; see
``substitution.splice``.

The cavity's usable ceiling is set by its first mode, which is a function of
its largest internal dimension. ``max_valid_hz`` computes it -- exceeding it
silently produces position-dependent data, which defeats the entire purpose.

Status: Milestone 2. Not yet implemented.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Speed of sound at 20 C, m/s.
SPEED_OF_SOUND_MS = 343.0

#: Stay this far below the first mode; the mode is not a brick wall.
MODE_SAFETY_FACTOR = 0.5


@dataclass(frozen=True)
class CouplerGeometry:
    """Internal dimensions of the sealed cavity, in metres."""

    length_m: float
    width_m: float
    height_m: float

    @property
    def largest_dimension_m(self) -> float:
        return max(self.length_m, self.width_m, self.height_m)


def first_mode_hz(geometry: CouplerGeometry) -> float:
    """Frequency of the cavity's first standing-wave mode."""
    return SPEED_OF_SOUND_MS / (2.0 * geometry.largest_dimension_m)


def max_valid_hz(geometry: CouplerGeometry) -> float:
    """Highest frequency for which the cavity gives position-independent data."""
    return first_mode_hz(geometry) * MODE_SAFETY_FACTOR


def verify_seal(geometry: CouplerGeometry) -> None:
    """Check the cavity is airtight before trusting low-frequency data.

    A leak behaves as a high-pass filter and will be mistaken for microphone
    roll-off -- precisely the quantity being measured.
    """
    raise NotImplementedError("Milestone 2")
