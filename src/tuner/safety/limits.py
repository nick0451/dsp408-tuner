"""Stimulus level limiting and abort conditions.

Every sample that reaches an output device passes through here. See the hard
safety rules in CLAUDE.md -- there is no bypass, including for quick tests.

The defaults in this module are deliberately conservative. A channel whose
crossover is unknown is treated as a tweeter, because that is the assumption
whose failure mode is a quiet measurement rather than a destroyed driver.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Every stimulus begins here and ramps toward its target.
START_LEVEL_DBFS = -30.0

#: Applied to any channel that has not been explicitly characterized. This is
#: the tweeter-safe assumption; see rule 4 in CLAUDE.md.
DEFAULT_CEILING_DBFS = -20.0

#: A sample magnitude at or above this counts as clipping.
CLIP_THRESHOLD = 0.999

#: Mean absolute offset above which a capture is treated as DC-contaminated.
DC_OFFSET_LIMIT = 0.01


class SafetyViolation(RuntimeError):
    """Raised when a stimulus or capture violates a safety rule.

    Always raised, never warned. A caller that continues past a safety
    condition is operating on a signal chain that is not what it believes it
    is, which is precisely the situation the rules exist to prevent.
    """


@dataclass(frozen=True)
class ChannelLimit:
    """Per-channel stimulus ceiling.

    Attributes:
        ceiling_dbfs: Maximum permitted stimulus level for this channel.
        characterized: Whether the channel's driver and crossover are known.
            An uncharacterized channel may not be raised above the default
            ceiling regardless of what is requested.
    """

    ceiling_dbfs: float = DEFAULT_CEILING_DBFS
    characterized: bool = False

    def __post_init__(self) -> None:
        if not self.characterized and self.ceiling_dbfs > DEFAULT_CEILING_DBFS:
            raise SafetyViolation(
                f"ceiling {self.ceiling_dbfs:.1f} dBFS requested for an "
                f"uncharacterized channel; maximum is "
                f"{DEFAULT_CEILING_DBFS:.1f} dBFS. Characterize the channel "
                f"before raising its ceiling."
            )


def check_ceiling(level_dbfs: float, limit: ChannelLimit) -> None:
    """Raise if ``level_dbfs`` exceeds the channel's ceiling."""
    if level_dbfs > limit.ceiling_dbfs:
        raise SafetyViolation(
            f"stimulus level {level_dbfs:.1f} dBFS exceeds channel ceiling "
            f"{limit.ceiling_dbfs:.1f} dBFS"
        )


def ramp_levels_dbfs(target_dbfs: float, steps: int = 4) -> list[float]:
    """Level sequence from START_LEVEL_DBFS up to ``target_dbfs``.

    The ramp is the mechanism by which a misrouted channel or an unexpectedly
    efficient driver is discovered while it is still quiet. Callers measure at
    each step and abort on any violation before proceeding to the next.
    """
    if target_dbfs < START_LEVEL_DBFS:
        return [target_dbfs]
    if steps < 1:
        raise ValueError("steps must be >= 1")
    return list(np.linspace(START_LEVEL_DBFS, target_dbfs, steps + 1))


def assert_capture_sane(capture: np.ndarray, channel: int | None = None) -> None:
    """Raise if a captured buffer shows clipping or DC offset.

    Both conditions mean the signal chain differs from what the caller assumes.
    """
    where = f" on channel {channel}" if channel is not None else ""

    peak = float(np.max(np.abs(capture))) if capture.size else 0.0
    if peak >= CLIP_THRESHOLD:
        raise SafetyViolation(f"clipping detected{where} (peak {peak:.4f})")

    offset = float(np.mean(capture)) if capture.size else 0.0
    if abs(offset) > DC_OFFSET_LIMIT:
        raise SafetyViolation(f"DC offset detected{where} ({offset:+.4f})")


def apply(samples: np.ndarray, level_dbfs: float, limit: ChannelLimit) -> np.ndarray:
    """Scale ``samples`` to ``level_dbfs`` after checking it against ``limit``.

    This is the only sanctioned path from generated stimulus to output device.
    """
    check_ceiling(level_dbfs, limit)

    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak == 0.0:
        return samples.astype(np.float64, copy=True)

    # Normalize to unity peak first so the requested level is meaningful
    # regardless of the generator's output convention.
    scaled = samples.astype(np.float64) / peak * (10.0 ** (level_dbfs / 20.0))

    if float(np.max(np.abs(scaled))) >= CLIP_THRESHOLD:
        raise SafetyViolation("scaled stimulus would clip")

    return scaled


def ceiling_for_device_state(
    channel_gain_dbfs: float,
    peq_gains_dbfs: tuple[float, ...] | list[float] = (),
    driver_ceiling_dbfs: float = DEFAULT_CEILING_DBFS,
) -> ChannelLimit:
    """A stimulus ceiling derived from what the DSP will *add* downstream.

    **This is hard safety rule 6, moved from prose into code.**

    The limiter caps what we transmit. The driver receives that plus the
    device's channel gain and whatever EQ boost is configured, and nothing else
    in :mod:`tuner.safety` can see either -- they live inside the DSP, after us.
    A +12 dB band on a channel at +6 dB turns a -18 dBFS stimulus into 0 dBFS at
    the speaker.

    Until now the correction was arithmetic the operator did by hand, every
    time, from memory. That was survivable while a human chose each level and
    typed each command. **It stops being survivable in the closed loop**, which
    is precisely the sequence where the optimizer writes a boost to a channel
    and then immediately sweeps that same channel -- with no human in between to
    remember. Of everything the tuning run can get wrong, this is the one that
    destroys hardware rather than producing a bad answer.

    So: read the gain and the band levels back from the device, pass them here,
    and sweep at the ceiling this returns.

    Args:
        channel_gain_dbfs: The output's own gain, in dB. Positive raises.
        peq_gains_dbfs: Every band's gain. **All of them**, not the enabled
            ones -- a band at 0 dB contributes nothing and costs nothing to
            include, and filtering the list is a chance to filter wrongly.
        driver_ceiling_dbfs: What the *driver* can take. Defaults to the
            tweeter-safe assumption, as everywhere else.

    Returns:
        A :class:`ChannelLimit` whose ceiling is the driver's, less everything
        the device adds after us.

    The worst case is summed, not maximised. Overlapping bands add, and while
    assuming every band peaks together is pessimistic, the cost of being
    pessimistic is a quieter sweep and the cost of being optimistic is a
    tweeter. ``characterized`` is set only when the caller has raised the
    driver ceiling deliberately, so the result cannot be used to sneak past
    :class:`ChannelLimit`'s own guard.
    """
    boost = sum(g for g in peq_gains_dbfs if g > 0.0)
    downstream = float(channel_gain_dbfs) + boost
    ceiling = float(driver_ceiling_dbfs) - max(downstream, 0.0)
    return ChannelLimit(
        ceiling_dbfs=ceiling,
        # Only a deliberately raised driver ceiling counts as characterization.
        # The tempting extra clause -- "or the computed ceiling came out at or
        # below the default" -- is unnecessary, because ChannelLimit's guard
        # only fires *above* the default. Including it made the flag read True
        # for a channel nobody had characterized, which relaxes nothing today
        # and misreports the one fact rule 4 turns on.
        characterized=driver_ceiling_dbfs > DEFAULT_CEILING_DBFS,
    )
