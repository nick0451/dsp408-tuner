"""Stimulus level limiting and abort conditions.

The only sanctioned path from generated stimulus to an output device.
"""

from .limits import (
    DEFAULT_CEILING_DBFS,
    START_LEVEL_DBFS,
    CaptureLevel,
    ChannelLimit,
    SafetyViolation,
    apply,
    assert_capture_sane,
    ceiling_for_device_state,
    check_ceiling,
    inspect_capture,
    ramp_levels_dbfs,
)

__all__ = [
    "DEFAULT_CEILING_DBFS",
    "START_LEVEL_DBFS",
    "ChannelLimit",
    "SafetyViolation",
    "apply",
    "CaptureLevel",
    "assert_capture_sane",
    "inspect_capture",
    "ceiling_for_device_state",
    "check_ceiling",
    "ramp_levels_dbfs",
]
