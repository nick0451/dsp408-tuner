"""Measurement: stimulus, deconvolution, gating, derived metrics."""

from .capture import CaptureConfig, SessionInfo, capture_sweep
from .deconv import arrival_offset_samples, deconvolve, peak_index
from .gating import gate, valid_above_hz, window_for_lowest_hz
from .metrics import (
    frequency_response,
    group_delay_samples,
    log_freqs,
    magnitude_db,
    octave_bands,
    phase_rad,
    rt60,
    rt60_from_impulse,
    spatial_average,
)
from .qa import (
    IndeterminateLinearity,
    LinearityResult,
    NonLinearPath,
    measure_level_linearity,
    require_linear_path,
)
from .result import (
    Coupling,
    IncomparableProvenance,
    Measurement,
    NoTimingReference,
    Provenance,
)
from .sweep import Sweep, log_sweep

__all__ = [
    "CaptureConfig",
    "Coupling",
    "IncomparableProvenance",
    "LinearityResult",
    "Measurement",
    "NoTimingReference",
    "IndeterminateLinearity",
    "NonLinearPath",
    "Provenance",
    "SessionInfo",
    "Sweep",
    "arrival_offset_samples",
    "capture_sweep",
    "deconvolve",
    "frequency_response",
    "gate",
    "group_delay_samples",
    "log_freqs",
    "log_sweep",
    "magnitude_db",
    "measure_level_linearity",
    "octave_bands",
    "peak_index",
    "phase_rad",
    "require_linear_path",
    "rt60",
    "rt60_from_impulse",
    "spatial_average",
    "valid_above_hz",
    "window_for_lowest_hz",
]
