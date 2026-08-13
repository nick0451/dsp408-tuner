"""Simulated DSP backend -- the development and test default.

Desktop development and the entire test suite run against this. No hardware is
required to work on any part of this project except the hardware backends.

The simulation's job is to enforce the same resource constraints the real chip
does, so that code which passes here does not discover on first contact with
hardware that its tune does not fit.
"""

from __future__ import annotations

from .backend import (
    BudgetExceeded,
    ChannelConfig,
    DeviceLimits,
    DspBackend,
)


class SimulatedDsp(DspBackend):
    """In-memory DSP that enforces the ADAU1701-shaped resource budget."""

    def __init__(self, limits: DeviceLimits | None = None) -> None:
        self._limits = limits or DeviceLimits()
        self._channels = [ChannelConfig() for _ in range(self._limits.n_outputs)]
        self._connected = False

    @property
    def limits(self) -> DeviceLimits:
        return self._limits

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("backend is not connected")

    def _check_output(self, output: int) -> None:
        if not 0 <= output < self._limits.n_outputs:
            raise IndexError(
                f"output {output} out of range (0..{self._limits.n_outputs - 1})"
            )

    def read_channel(self, output: int) -> ChannelConfig:
        self._require_connected()
        self._check_output(output)
        return self._channels[output]

    def write_channel(self, output: int, config: ChannelConfig) -> None:
        self._require_connected()
        self._check_output(output)

        if len(config.peq) > self._limits.max_peq_per_channel:
            raise BudgetExceeded(
                f"{len(config.peq)} PEQ bands requested on output {output}; "
                f"device allows {self._limits.max_peq_per_channel}"
            )

        if config.delay_samples < 0:
            raise ValueError("delay_samples must be non-negative")

        # Delay is allocated **per output**, measured 2026-08-12: the vendor
        # app offers its full 8 ms on every output at once, which a shared
        # pool could not do. This simulator enforced a 1024-sample per-chip
        # pool until that measurement, and was wrong in both directions.
        if config.delay_samples > self._limits.max_delay_samples_per_output:
            raise BudgetExceeded(
                f"delay of {config.delay_samples} samples on output "
                f"{output + 1} exceeds this device's per-output ceiling of "
                f"{self._limits.max_delay_samples_per_output} "
                f"({self._limits.max_delay_samples_per_output / 48.0:.0f} ms "
                f"at 48 kHz). Delay is not pooled -- no other output's "
                f"headroom is relevant, and freeing one will not help."
            )

        self._channels[output] = config

    def total_delay_samples(self) -> int:
        """Delay currently committed across the whole device.

        Informational only, and now informational for a different reason than
        it used to be: delay is allocated per output, so a device total is not
        a budget because there is no shared budget to total.
        """
        return sum(ch.delay_samples for ch in self._channels)

    def delay_samples_on(self, chip: int) -> int:
        """Delay committed on one chip.

        Retained because the chip map is a measured fact worth being able to
        query, but it **no longer binds anything**: delay is per output. Do not
        reintroduce a comparison against a per-chip pool -- there isn't one.
        """
        return sum(
            self._channels[o].delay_samples for o in self._limits.outputs_on(chip)
        )
