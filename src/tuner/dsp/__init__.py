"""DSP control backends.

``SimulatedDsp`` is the default; hardware backends are selected explicitly.
"""

from .backend import (
    Biquad,
    BudgetExceeded,
    ChannelConfig,
    Crossover,
    DeviceLimits,
    DspBackend,
    FilterType,
)
from .ddp import (
    DdpBackup,
    DdpEqBand,
    DdpOutput,
)
from .ddp import diff as ddp_diff
from .ddp import parse as ddp_parse
from .protocol import (
    DataType,
    DestructiveCommand,
    EqBand,
    Frame,
    FrameType,
    OutputBlock,
    OutputDynamics,
    OutputMisc,
    OutputXover,
    ProtocolError,
    decode,
    nearest_eq_index,
    nearest_xover_index,
    read_output,
    write_output_dynamics,
    write_output_eq,
    write_output_misc,
    write_output_mix,
    write_output_xover,
)
from .sim import SimulatedDsp

__all__ = [
    "Biquad",
    "BudgetExceeded",
    "ChannelConfig",
    "Crossover",
    "DataType",
    "DdpBackup",
    "DdpEqBand",
    "DdpOutput",
    "DestructiveCommand",
    "DeviceLimits",
    "DspBackend",
    "EqBand",
    "FilterType",
    "Frame",
    "FrameType",
    "OutputBlock",
    "OutputDynamics",
    "OutputMisc",
    "OutputXover",
    "ProtocolError",
    "SimulatedDsp",
    "ddp_diff",
    "ddp_parse",
    "decode",
    "nearest_eq_index",
    "nearest_xover_index",
    "read_output",
    "write_output_dynamics",
    "write_output_eq",
    "write_output_misc",
    "write_output_mix",
    "write_output_xover",
]
