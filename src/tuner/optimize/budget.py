"""Hardware resource accounting for the optimizer.

The optimizer solves against the device's resource budget as a **hard
constraint**, not as a post-hoc check. Producing an attractive target-matching
curve that cannot be loaded onto the chip is a failure, not a partial success.

**Delay is allocated per channel, not pooled.** Measured 2026-08-12 by the
simplest possible experiment: the vendor app offers its full 8 ms maximum on
every output *simultaneously*. A shared pool would have to shrink one
channel's ceiling as its neighbours consumed it, and it does not.

This module previously modelled a 1024-sample pool shared across each chip's
four outputs, and that was wrong in both directions -- it would have permitted
1024 samples on a single output the device caps at 384, and refused a
four-channel total of 1200 the device runs happily. The chip map is still a
measured fact and still lives on :class:`DeviceLimits`; it simply turns out
that no resource we can spend is pooled by it.

The docstring here used to warn that "the per-channel API makes each output
look independent; the silicon does not." That reads the other way round now:
the per-channel API makes each output look independent **because it is**.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..dsp.backend import ChannelConfig, DeviceLimits


@dataclass(frozen=True)
class OutputUsage:
    """Delay consumed on one output, against that output's own ceiling."""

    output: int
    delay_samples_used: int
    delay_samples_available: int

    @property
    def delay_headroom_samples(self) -> int:
        return self.delay_samples_available - self.delay_samples_used

    @property
    def fits(self) -> bool:
        return 0 <= self.delay_samples_used <= self.delay_samples_available


@dataclass(frozen=True)
class BudgetUsage:
    """Resources consumed by a complete multi-channel configuration."""

    outputs: tuple[OutputUsage, ...]
    peq_bands_used: tuple[int, ...]

    @property
    def delay_samples_used(self) -> int:
        """Delay committed across the whole device."""
        return sum(o.delay_samples_used for o in self.outputs)

    @property
    def delay_headroom_samples(self) -> int:
        """Headroom on the **tightest output**, which is the one that binds.

        Deliberately the minimum rather than the sum, for the same reason the
        per-chip version was: a device total would report plenty free while a
        tune is already unloadable because it is all wanted on one channel,
        and it would read as comfortable margin right up until the write
        failed.
        """
        return min(o.delay_headroom_samples for o in self.outputs)

    @property
    def fits(self) -> bool:
        return all(o.fits for o in self.outputs)

    def output(self, index: int) -> OutputUsage:
        return self.outputs[index]

    def over_budget(self) -> tuple[OutputUsage, ...]:
        """Only the outputs that do not fit, for an error message worth reading."""
        return tuple(o for o in self.outputs if not o.fits)

    def summary(self) -> str:
        worst = min(self.outputs, key=lambda o: o.delay_headroom_samples)
        return (
            f"delay: {self.delay_samples_used} samples across "
            f"{len(self.outputs)} outputs; tightest is output "
            f"{worst.output + 1} at {worst.delay_samples_used}/"
            f"{worst.delay_samples_available}"
        )


def account(
    channels: list[ChannelConfig],
    limits: DeviceLimits,
) -> BudgetUsage:
    """Resources a configuration would consume, **per output**.

    ``channels`` is indexed by output and must cover every output the device
    has. That requirement predates the per-channel delay model and is kept:
    a short list is a caller mistake worth catching, and `peq_bands_used` is
    reported for the whole device.
    """
    if len(channels) != limits.n_outputs:
        raise ValueError(
            f"account() needs one config per output: got {len(channels)} for "
            f"{limits.n_outputs} outputs."
        )
    return BudgetUsage(
        outputs=tuple(
            OutputUsage(
                output=o,
                delay_samples_used=channels[o].delay_samples,
                delay_samples_available=limits.max_delay_samples_per_output,
            )
            for o in range(limits.n_outputs)
        ),
        peq_bands_used=tuple(len(ch.peq) for ch in channels),
    )


def normalize_delays(channels: list[ChannelConfig]) -> list[ChannelConfig]:
    """Subtract the common delay offset across all channels.

    Time alignment only ever needs *relative* delay, so the smallest value can
    always be brought to zero. That is worth doing even now that delay is
    allocated per channel rather than pooled: the per-output ceiling is 384
    samples, and an unnormalised set can exceed it for no reason other than a
    constant nobody needs.

    **Normalise across every output, never per chip.** Two chips do not make
    two independent time references -- the subwoofers on one have to stay
    aligned with the mids on the other, and subtracting each chip's own
    minimum would shift them relative to each other.
    """
    if not channels:
        return []
    # A delay line cannot run backwards. If alignment produced a negative,
    # the alignment is wrong rather than merely unnormalised, and subtracting
    # the minimum would silently paper over it.
    negative = [ch for ch in channels if ch.delay_samples < 0]
    if negative:
        raise ValueError(
            f"{len(negative)} channel(s) have a negative delay; the smallest "
            f"is {min(ch.delay_samples for ch in negative)}. Normalising "
            f"would hide it."
        )
    offset = min(ch.delay_samples for ch in channels)
    if offset == 0:
        return list(channels)
    return [replace(ch, delay_samples=ch.delay_samples - offset) for ch in channels]
