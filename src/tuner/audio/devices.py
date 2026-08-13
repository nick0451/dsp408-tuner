"""Audio device enumeration and capability checks.

Multichannel capture reliability is one of the two real risks in this project
(the DSP control protocol, the other one, is now decided). Multichannel USB
interfaces are inconsistent about delivering all their inputs at once --
reboots on plug-in and partially-working capture are both documented failure
modes on ARM hosts. ``verify_simultaneous_capture`` is a milestone-1
acceptance gate, not an optional diagnostic: a partially-working interface is
worse than a broken one, because it produces data that looks fine.

The target interface is a purpose-built 6-in/4-out device on a single clock;
see docs/measurement-interface.md. It does not exist yet, and this gate is
needed for any multichannel interface, bought or built.

Status: Milestone 1. Not yet implemented.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    index: int
    max_input_channels: int
    max_output_channels: int
    default_sample_rate_hz: float


def list_devices() -> list[DeviceInfo]:
    """Enumerate available audio devices."""
    raise NotImplementedError("Milestone 1")


def verify_simultaneous_capture(
    device: str | int,
    n_channels: int,
    sample_rate_hz: int,
    duration_s: float = 5.0,
) -> None:
    """Assert that ``n_channels`` capture cleanly and stay sample-aligned.

    Checks for dropouts and inter-channel alignment. Raises on failure with
    detail on which channel misbehaved -- a partially-working interface is
    worse than a non-working one because it produces data that looks fine.
    """
    raise NotImplementedError("Milestone 1")
