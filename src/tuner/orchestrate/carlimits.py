"""Per-channel hard limits for the development car, checked rather than trusted.

[docs/car-system.md](../../../docs/car-system.md) has held this system's driver
specs and clip limits as prose since 2026-08-14. Prose does not refuse a write.
This module is the same facts in a form that does.

**The drivers on this car have no passive crossovers.** The DSP's filter is the
only thing between a full-range signal and a 1-inch tweeter, so a crossover
that is wrong is not a tonal problem, it is a dead driver. Every limit here
exists because something specific breaks without it.

Two things this deliberately does *not* do:

* **It does not check routing.** Which output drives which driver is the
  operator's declaration, recorded in ``ChannelSpec.basis``. Verifying it by
  measurement was tried and cost more session time than it returned; the
  operator knows their own install. What is interrogated instead is the
  *driver* -- its corner, its slope, its level -- because that is where an
  error destroys hardware rather than merely confusing a fit.
* **It cannot read input gain.** ``DataType 3`` has never been reached by any
  vendor app capture, so the 0-100 input level the iOS app shows is invisible
  to us. The limit is recorded and must be declared by the operator; see
  :func:`require_declared_input_gain`.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Master volume, ``DataType 9`` channel 5 byte 0. Operator-declared 2026-08-14:
#: the unit clips above 54, and a tuning run sits at 48. Read live at 51 the
#: same day, which is consistent with the app's 0-60 scale being this byte
#: directly -- **consistent with, not confirmed**; nobody has yet read the app
#: and this byte at the same moment.
MASTER_VOLUME_MAX = 54

#: Input gain, operator-declared clip point on the iOS app's 0-100 scale.
#: **Unreadable and unwritable by us** -- see the module docstring.
INPUT_GAIN_MAX = 90

#: Output channel gain ceiling, in dBFS. The operator's "output channels clip
#: past 54" on the iOS 0-60 scale, converted through ``protocol.gain_dbfs``
#: (``dB = raw/10 - 60``), which puts 54 at exactly -6.0 dB.
MAX_OUTPUT_GAIN_DBFS = -6.0

#: A band whose level differs from 0 dB by more than this counts as not flat.
#: One quantisation step of ``EqBand.level`` is 0.1 dB.
FLAT_TOLERANCE_DB = 0.05


@dataclass(frozen=True)
class ChannelSpec:
    """What must be true of one output before a stimulus may reach it.

    Attributes:
        output: Zero-based DSP output index.
        driver: What is connected, in the operator's words.
        high_pass_hz: The corner the channel must be high-passed at. ``None``
            means no high-pass requirement -- only correct for a subwoofer
            channel whose subsonic filter is expressed separately.
        low_pass_hz: The corner the channel must be low-passed at.
        min_slope_db_oct: The **floor**, below which a stimulus is refused.
            For the GB10 tweeters this is the manufacturer's stated minimum.
        recommended_slope_db_oct: Warned about, never refused. 12 dB/oct holds
            a tweeter's excursion *constant* below its corner rather than
            reducing it; 24 dB/oct makes it fall.
        max_gain_dbfs: Channel gain ceiling.
        basis: The operator's claim about what is connected. Unverifiable by
            code, recorded in provenance, same family as ``DriverCeiling.basis``.
    """

    output: int
    driver: str
    high_pass_hz: int | None
    low_pass_hz: int | None
    min_slope_db_oct: int
    recommended_slope_db_oct: int
    max_gain_dbfs: float
    basis: str

    @property
    def label(self) -> str:
        return f"OUT{self.output + 1}"


#: The development car, from ``docs/car-system.md``. Every row is
#: operator-declared; the driver specs behind the slope floors are cited there.
CAR_DSP408: tuple[ChannelSpec, ...] = (
    *(
        ChannelSpec(
            output=o,
            driver="FaitalPRO 4FE35, 4in midrange",
            high_pass_hz=450,
            low_pass_hz=3500,
            min_slope_db_oct=12,
            recommended_slope_db_oct=12,
            max_gain_dbfs=MAX_OUTPUT_GAIN_DBFS,
            basis="operator, 2026-08-14. Fs 100 Hz, Xmax 1.73 mm, 30 W RMS -- "
            "450 Hz is 4.5x Fs and there is no excursion headroom to spend "
            "on a lower corner",
        )
        for o in (0, 1)
    ),
    *(
        ChannelSpec(
            output=o,
            driver="Audiofrog GB10, 1in tweeter",
            high_pass_hz=3500,
            low_pass_hz=20000,
            min_slope_db_oct=12,
            recommended_slope_db_oct=24,
            max_gain_dbfs=MAX_OUTPUT_GAIN_DBFS,
            basis="operator, 2026-08-14, unfiltered. Manufacturer requires a "
            "high-pass of at least 2.5 kHz at at least 12 dB/oct, and states "
            "both power ratings as conditional on it",
        )
        for o in (2, 3)
    ),
    *(
        ChannelSpec(
            output=o,
            driver="CT Sounds Meso 6.5, midbass",
            high_pass_hz=65,
            low_pass_hz=450,
            min_slope_db_oct=12,
            recommended_slope_db_oct=12,
            max_gain_dbfs=MAX_OUTPUT_GAIN_DBFS,
            basis="operator, 2026-08-14. Which Meso variant is UNRESOLVED; the "
            "subwoofer version publishes Fs 56.3 Hz, which would put this 65 Hz "
            "corner essentially on resonance. Do not lower it",
        )
        for o in (4, 5)
    ),
    *(
        ChannelSpec(
            output=o,
            driver="subwoofer, two drivers in one ported box",
            high_pass_hz=20,
            low_pass_hz=65,
            min_slope_db_oct=12,
            recommended_slope_db_oct=24,
            max_gain_dbfs=MAX_OUTPUT_GAIN_DBFS,
            basis="operator, 2026-08-10. Ported, so below tuning the box "
            "unloads and excursion runs away; the 20 Hz high-pass is the "
            "subsonic filter that prevents it",
        )
        for o in (6, 7)
    ),
)

SPEC_BY_OUTPUT = {spec.output: spec for spec in CAR_DSP408}


@dataclass(frozen=True)
class Violation:
    """One thing that is not as it must be.

    ``fatal`` violations refuse the run. Non-fatal ones are printed and
    proceed -- they are recommendations, and turning a recommendation into a
    refusal is how a safety gate trains its operator to bypass it.
    """

    where: str
    message: str
    fatal: bool


def slope_db_oct(level_raw: int) -> int:
    """Crossover slope from the stored level byte. ``slope = 6 * (level + 1)``."""
    return 6 * (level_raw + 1)


def check_channel(spec: ChannelSpec, config, *, require_flat: bool) -> list[Violation]:
    """Everything that must hold for one output, against a live read.

    ``config`` is a :class:`~tuner.dsp.backend.ChannelConfig`.
    """
    out: list[Violation] = []
    w = spec.label
    xo = config.crossover

    for end, want, actual in (
        ("high-pass", spec.high_pass_hz, xo.high_pass_hz),
        ("low-pass", spec.low_pass_hz, xo.low_pass_hz),
    ):
        if want is None:
            continue
        if actual is None:
            out.append(Violation(
                w,
                f"{end} is DISABLED, must be {want} Hz for {spec.driver}",
                fatal=True,
            ))
        elif int(actual) != want:
            out.append(Violation(
                w,
                f"{end} is {actual:.0f} Hz, must be {want} Hz for "
                f"{spec.driver}",
                fatal=True,
            ))

    # ``Crossover`` carries one slope for both ends, which is what the device
    # record stores per corner but the model flattens. Checking the single
    # value is therefore checking both.
    if xo.slope_db_oct < spec.min_slope_db_oct:
        out.append(Violation(
            w,
            f"slope {xo.slope_db_oct} dB/oct is below the "
            f"{spec.min_slope_db_oct} dB/oct floor for {spec.driver}",
            fatal=True,
        ))
    elif xo.slope_db_oct < spec.recommended_slope_db_oct:
        out.append(Violation(
            w,
            f"slope {xo.slope_db_oct} dB/oct meets the floor but "
            f"{spec.recommended_slope_db_oct} is recommended -- at "
            f"{xo.slope_db_oct} dB/oct excursion below the corner is held "
            f"constant rather than reduced",
            fatal=False,
        ))

    if config.gain_dbfs > spec.max_gain_dbfs:
        out.append(Violation(
            w,
            f"gain {config.gain_dbfs:+.1f} dBFS exceeds the "
            f"{spec.max_gain_dbfs:+.1f} dBFS ceiling",
            fatal=True,
        ))

    if require_flat:
        loaded = [
            b for b in config.peq if abs(b.gain_dbfs) > FLAT_TOLERANCE_DB
        ]
        if loaded:
            worst = max(loaded, key=lambda b: abs(b.gain_dbfs))
            out.append(Violation(
                w,
                f"{len(loaded)} EQ band(s) loaded, largest "
                f"{worst.gain_dbfs:+.1f} dB at {worst.freq_hz:.0f} Hz; "
                f"a baseline must be measured flat",
                fatal=True,
            ))
    return out


def check_master_volume(system_blocks: dict[int, bytes]) -> list[Violation]:
    """Master volume against the operator's declared clip point.

    Global: one write moves every output at once, and it is in no channel
    record. See ``snapshot.compare_system``.
    """
    from ..dsp.snapshot import SYSTEM_MASTER_VOLUME

    block = system_blocks.get(SYSTEM_MASTER_VOLUME)
    if not block:
        return [Violation(
            "master", "master volume block not present in the handshake", True
        )]
    value = block[0]
    if value > MASTER_VOLUME_MAX:
        return [Violation(
            "master",
            f"master volume is {value}, above the declared clip point of "
            f"{MASTER_VOLUME_MAX}",
            fatal=True,
        )]
    return []


def require_declared_input_gain(declared: int | None) -> list[Violation]:
    """Input gain is unreadable, so it must be declared and is then checked.

    Refusing to proceed without a declaration is the point. Treating "unknown"
    as "probably fine" is how an input stage runs clipped through an entire
    tuning session while every output-side check passes.
    """
    if declared is None:
        return [Violation(
            "input",
            "input gain is unreadable over our protocol (DataType 3 is "
            "unmapped) and was not declared. Read it in the iOS app and pass "
            "it, or state that it cannot be read",
            fatal=True,
        )]
    if declared > INPUT_GAIN_MAX:
        return [Violation(
            "input",
            f"declared input gain {declared} is above the {INPUT_GAIN_MAX} "
            f"clip point",
            fatal=True,
        )]
    return []
