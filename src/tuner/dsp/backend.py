"""Abstract DSP control interface.

All DSP access goes through :class:`DspBackend`. Implementations are
:mod:`tuner.dsp.sim` (the default, used by the whole test suite) and
:mod:`tuner.dsp.dsp408_spp` (the real device, proven on hardware through
bring-up Stage 6 on 2026-08-11: reads, a verified single-block change and
rollback, then 46 writes across four channels including whole-channel
``write_channel`` and a mirrored gang write, with the device byte-identical to
a morning snapshot afterwards).

The transport is **classic Bluetooth RFCOMM (SPP), server channel 1**, and the
wire protocol is decoded and validated against real device traffic; see
:mod:`tuner.dsp.protocol` and docs/dsp408-protocol.md.

This interface kept the choice reversible while several candidate routes were
open, and it earned that twice over. The project committed to BLE on the
strength of a GATT scan, wrote that down as settled, and was wrong: an HCI
capture of the vendor app showed RFCOMM and no BLE at all. Nothing above this
boundary had to change.

ADAU1701-specific and transport-specific details must not leak above this
boundary. Callers work in engineering units; the backend owns the translation
to raw device values.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from ..safety.limits import (
    DEFAULT_CEILING_DBFS,
    ChannelLimit,
    ceiling_for_device_state,
)


class FilterType(Enum):
    PEAKING = "peaking"
    LOW_SHELF = "low_shelf"
    HIGH_SHELF = "high_shelf"
    LOW_PASS = "low_pass"
    HIGH_PASS = "high_pass"


@dataclass(frozen=True)
class Biquad:
    """One parametric EQ band, in engineering units rather than coefficients.

    Coefficient computation is the backend's job -- different targets quantize
    differently, and the optimizer should reason about frequency/gain/Q.
    """

    freq_hz: float
    gain_dbfs: float
    q: float
    kind: FilterType = FilterType.PEAKING
    enabled: bool = True


@dataclass(frozen=True)
class Crossover:
    """Per-output crossover. ``None`` disables that end."""

    high_pass_hz: float | None = None
    low_pass_hz: float | None = None
    slope_db_oct: int = 24


@dataclass(frozen=True)
class ChannelConfig:
    """Complete state of one DSP output channel."""

    gain_dbfs: float = 0.0
    delay_samples: int = 0
    crossover: Crossover = Crossover()
    peq: tuple[Biquad, ...] = ()
    muted: bool = False


#: Which ADAU1701 each output is driven by, indexed by zero-based output.
#:
#: **Measured 2026-08-11**, not guessed. Every output was stepped one gain
#: click while the ADAU control bus was captured, and the chip that received
#: the write recorded: ``0x37`` takes outputs 1-4, ``0x35`` takes 5-8. Outputs
#: 1, 2 and 7/8 are pinned by unique coefficient values and need no inference;
#: 3-6 shared a value, so their attribution was **falsified** by re-running
#: them in reverse order and confirming the assignment followed the channel
#: rather than the sequence position. See docs/dsp408-protocol.md.
#:
#: This is the grouping the project spent weeks declining to guess. It turned
#: out to be the obvious one, which is exactly why it was worth measuring.
OUTPUT_CHIP: tuple[int, ...] = (0, 0, 0, 0, 1, 1, 1, 1)


@dataclass(frozen=True)
class DeviceLimits:
    """Hardware resource ceilings the optimizer must solve within.

    **Delay is allocated per output, not pooled.** Measured 2026-08-12: the
    vendor app offers its full 8 ms maximum on every output simultaneously. A
    shared pool would have to shrink one channel's ceiling as its neighbours
    consumed it, and it does not. 8 ms at 48 kHz is
    :attr:`max_delay_samples_per_output` = 384.

    This class carried ``delay_samples_per_chip = 1024`` until that
    measurement. It was wrong in **both** directions: it would have permitted
    1024 samples on a single output the device caps at 384, and refused a
    four-channel total of 1200 the device runs happily. The earlier docstring
    also warned that "the per-channel API makes every output look independent"
    when the silicon did not -- which turns out to be backwards. They are.

    The **chip map is still measured and still here**: outputs 1-4 are driven
    by the ADAU at I2C ``0x37`` and 5-8 by the one at ``0x35``, confirmed on a
    logic analyser and falsified by a reverse-order re-run. It simply turns out
    that no resource we can spend is pooled by it.

    .. note::
       **Program space is deliberately not modelled**, and that is now a
       considered position rather than a gap. The DSP-408 runs fixed vendor
       firmware; we write coefficients into slots that already exist and
       execute whether we use them or not. Setting a band flat writes unity
       coefficients, it does not free an instruction. There is no configuration
       we can send that exhausts program space, because program space is not a
       resource we allocate.

       An earlier version of this docstring asserted that "two ADAU1701s will
       not run 31 biquads on each of eight channels" -- an uncited ADAU1701
       claim from memory, in a project whose rules forbid exactly that. It has
       been removed rather than sourced, because the conclusion it supported
       was reached the wrong way even though the number it defended was right.

    .. warning::
       :attr:`max_peq_per_channel` is **10, measured acoustically** on
       2026-08-12, not merely observed in the vendor UI. Slot 1 realises a
       written band to 0.062 dB rms; slots 11 and 31 do **nothing at all**,
       across four runs, while reading back byte-exact.

       The device *addresses* 31 slots and the record stores all 31, which is
       why the enthusiast community has long believed there are thirty usable
       bands. There are ten. The remaining slots accept writes, ack them, and
       return them faithfully to a readback -- **a verified write is not a
       working write on this device** -- so nothing short of a measurement can
       tell them apart. See ``CLAUDE.md``.
    """

    sample_rate_hz: int = 48_000
    n_outputs: int = 8
    n_inputs: int = 4
    n_chips: int = 2
    max_peq_per_channel: int = 10
    max_delay_samples_per_output: int = 384
    output_chip: tuple[int, ...] = OUTPUT_CHIP

    #: True since 2026-08-12. Every field above is now measured: the sample
    #: rate and channel counts from the datasheet and the wire, the chip map
    #: on a logic analyser, and the two ceilings from the vendor UI. Program
    #: space is absent by decision rather than omission -- see the note above.
    measured: bool = True

    def __post_init__(self) -> None:
        if len(self.output_chip) != self.n_outputs:
            raise ValueError(
                f"output_chip has {len(self.output_chip)} entries for "
                f"{self.n_outputs} outputs"
            )
        if self.output_chip and max(self.output_chip) >= self.n_chips:
            raise ValueError(
                f"output_chip names chip {max(self.output_chip)} but there "
                f"are only {self.n_chips} chips"
            )

    def chip_of(self, output: int) -> int:
        """Which chip drives ``output``."""
        if not 0 <= output < self.n_outputs:
            raise IndexError(f"output {output} out of range")
        return self.output_chip[output]

    def outputs_on(self, chip: int) -> tuple[int, ...]:
        """Every output driven by ``chip``, ascending."""
        return tuple(o for o, c in enumerate(self.output_chip) if c == chip)


class DspBackend(ABC):
    """Control surface for a DSP. Implemented by sim and hardware routes."""

    @property
    @abstractmethod
    def limits(self) -> DeviceLimits:
        """Resource ceilings for this device."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def read_channel(self, output: int) -> ChannelConfig:
        """Current configuration of one output channel."""

    @abstractmethod
    def write_channel(self, output: int, config: ChannelConfig) -> None:
        """Apply ``config`` to one output channel.

        Implementations validate against :attr:`limits` and raise rather than
        silently truncating. A tune that does not fit the chip is not a tune.
        """

    def stimulus_limit(
        self, output: int, driver_ceiling_dbfs: float = DEFAULT_CEILING_DBFS
    ) -> ChannelLimit:
        """Stimulus ceiling for sweeping ``output``, given what the DSP adds.

        **Hard safety rule 6.** The safety limiter caps what we transmit; the
        driver receives that plus this channel's gain and EQ boost, both of
        which live inside the DSP where :mod:`tuner.safety` cannot see them.
        The backend can see them, so the backend is where they are read.

        Live, every time it is called. Caching would be the natural
        optimisation and it is the wrong one: the closed loop's whole shape is
        *write a boost, then sweep the channel you just boosted*, and a cached
        gain is a gain from before the write.

        ``driver_ceiling_dbfs`` defaults to the tweeter-safe assumption, which
        is the correct answer for any channel whose crossover is unknown.
        """
        config = self.read_channel(output)
        return ceiling_for_device_state(
            channel_gain_dbfs=config.gain_dbfs,
            peq_gains_dbfs=tuple(band.gain_dbfs for band in config.peq),
            driver_ceiling_dbfs=driver_ceiling_dbfs,
        )

    def __enter__(self) -> DspBackend:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()


class BudgetExceeded(ValueError):
    """Raised when a configuration does not fit the device's resources."""
