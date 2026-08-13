"""Measurement results and their provenance.

Two rules from CLAUDE.md are enforced here rather than left to callers:

1. **The timing-reference rule.** Phase and delay are unavailable without a
   hardware loopback. Accessing them raises rather than returning a plausible
   wrong number, because a wrong delay figure gets applied to the DSP.

2. **Provenance.** Measurements lacking comparable provenance cannot be
   diffed. Gain changes between sessions masquerade as acoustic differences,
   and so does anything that moved in the room -- which is why the strong
   term here is an operator-declared setup token rather than the thermometer
   reading this module used to gate on alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

import numpy as np

from .timing import TimingReference


class NoTimingReference(RuntimeError):
    """Raised when phase or delay is requested from a magnitude-only capture."""


class IncomparableProvenance(ValueError):
    """Raised when two measurements were not taken under comparable conditions."""


class Coupling(Enum):
    """How the measured signal reached the input.

    This exists because comparability is not one question. An **electrical**
    measurement -- DSP output straight into a line input -- has no propagation
    path, no room, and no microphone, so the environmental terms that dominate
    an acoustic comparison are not merely small, they are *absent*. Requiring
    them there does not make the check stricter; it makes it wrong, and the
    only way to satisfy it is to record a number that is not relevant to
    anything, which is worse than recording nothing.
    """

    #: Device output to interface input, by cable. No air involved.
    ELECTRICAL = "electrical"

    #: Through a loudspeaker, a room or cabin, and a microphone.
    ACOUSTIC = "acoustic"


@dataclass(frozen=True)
class Provenance:
    """Conditions under which a measurement was taken.

    Attributes:
        cal_file: Microphone calibration file used, if any.
        cal_sha256: Hash of that file's contents, so an edited cal file is
            detected rather than silently changing results.
        coupling: Electrical or acoustic. Decides which environmental terms
            participate in :meth:`comparable_to`. Defaults to
            :attr:`Coupling.ACOUSTIC`, the stricter reading, so a measurement
            that never says gets the conservative treatment.
        device: Interface and device identity string.
        gains_db: Per-input preamp gain settings.
        sample_rate_hz: Capture rate.
        injected_fault: Fingerprint of a deliberate fault filtered into the
            stimulus, for bench known-answer work. ``None`` for a real
            measurement. Gates comparability absolutely -- see
            :meth:`why_incomparable`.
        setup_token: The operator's verbatim claim about the physical setup.
            See :meth:`comparable_to`; required for acoustic measurements.
        temperature_c: Ambient temperature, for acoustic measurements.
        timestamp: When the capture was taken.
    """

    device: str
    sample_rate_hz: int
    gains_db: tuple[float, ...]
    timestamp: datetime
    cal_file: Path | None = None
    cal_sha256: str | None = None
    temperature_c: float | None = None
    coupling: Coupling = Coupling.ACOUSTIC
    setup_token: str | None = None
    injected_fault: str | None = None

    def __post_init__(self) -> None:
        if self.setup_token is not None:
            token = self.setup_token.strip()
            if not token:
                raise ValueError(
                    "setup_token is blank. It is a claim about the physical "
                    "configuration, typed by a person; an empty one asserts "
                    "nothing while making two measurements compare equal, "
                    "which is worse than omitting it"
                )
            # Canonicalize at construction, then compare verbatim. Trimming
            # the ends discards no information; anything more -- case folding,
            # collapsing internal runs -- would be leniency at comparison
            # time, and leniency is what makes two different setups match.
            object.__setattr__(self, "setup_token", token)

    def why_incomparable(
        self, other: Provenance, temp_tolerance_c: float = 2.0
    ) -> str | None:
        """The first reason these may not be diffed, or ``None`` if they may.

        Exists because ``INDETERMINATE`` is otherwise an opaque verdict. A run
        that stops after fitting and writing should say *which* term moved,
        not that something did.
        """
        if self.device != other.device:
            return f"different interface: {self.device!r} vs {other.device!r}"
        if self.sample_rate_hz != other.sample_rate_hz:
            return (
                f"different sample rate: {self.sample_rate_hz} vs "
                f"{other.sample_rate_hz} Hz"
            )
        if self.gains_db != other.gains_db:
            return f"different preamp gains: {self.gains_db} vs {other.gains_db}"
        if self.cal_sha256 != other.cal_sha256:
            return "different microphone calibration file (by content hash)"

        # Before anything environmental, because this one is not a matter of
        # degree. A capture with a deliberate fault filtered into its stimulus
        # is not a measurement of the system; it is a measurement of the
        # system *plus a known lie*, and the only thing it may be compared
        # with is another capture telling exactly the same lie.
        if self.injected_fault != other.injected_fault:
            here = self.injected_fault or "none"
            there = other.injected_fault or "none"
            return (
                f"different injected fault: {here} vs {there}. A capture with "
                f"a deliberate fault in its stimulus cannot be compared with a "
                f"clean one, or with a differently-faulted one"
            )
        if self.coupling is not other.coupling:
            return (
                f"different coupling: {self.coupling.value} vs "
                f"{other.coupling.value}; one went through air and the other "
                f"did not"
            )

        # The setup token gates both couplings, because a moved cable is as
        # real a change as a moved microphone. It is only *required* for
        # acoustic, where the environment it describes exists.
        if self.setup_token != other.setup_token:
            return (
                f"different setup token: {self.setup_token!r} vs "
                f"{other.setup_token!r}. The operator declared the physical "
                f"configuration changed between these measurements"
            )

        if self.coupling is Coupling.ELECTRICAL:
            return None

        if self.setup_token is None:
            return (
                "no setup token. An acoustic comparison needs the operator's "
                "claim that microphone position, seat position, doors, "
                "windows, HVAC and occupancy are unchanged -- none of which "
                "any code can check, and every one of which moves the "
                "response more than temperature does"
            )
        if self.temperature_c is None or other.temperature_c is None:
            return "temperature was not recorded for one or both measurements"
        drift_k = abs(self.temperature_c - other.temperature_c)
        if drift_k > temp_tolerance_c:
            return (
                f"temperature moved {drift_k:.1f} K, past the "
                f"{temp_tolerance_c:.1f} K tolerance"
            )
        return None

    def comparable_to(self, other: Provenance, temp_tolerance_c: float = 2.0) -> bool:
        """Whether two measurements may meaningfully be diffed.

        The signal-chain terms always apply: same interface, same rate, same
        preamp gains, same calibration file by hash. Change any of those and a
        difference in the curves says nothing about the tune.

        **The environmental terms participate only for acoustic
        measurements.** They were previously required of every measurement,
        which made an electrical bench comparison impossible to satisfy
        honestly -- the only way to pass was to record a room temperature for
        a cable, asserting the relevance of a variable that has none. An
        electrical path has no propagation delay to stretch and no room modes
        to move.

        **The setup token is the strong environmental term; temperature is
        the weak one.** The token is the operator's verbatim claim that the
        physical configuration is unchanged -- microphone position, seat
        position, doors, windows, HVAC, occupancy. No code can verify any of
        that, which is exactly why it is typed by a person and compared
        literally, in the same family as ``DriverCeiling.basis`` and
        ``NoIsolation.basis``. Two measurements with different tokens are
        incomparable whatever the thermometer says, and an acoustic
        measurement with no token at all cannot be compared to anything.

        A token gates an *electrical* comparison too when either side declares
        one, because a cable moved from OUT1 to OUT2 is a real change that no
        other field records. It is simply not required there, since the
        environment it describes is not in the signal path.

        .. note::
           **``temp_tolerance_c = 2.0`` is uncited**, and the acoustic case it
           guards is weaker than this project once wrote it. Temperature
           changes the speed of sound by roughly 0.17 %/K, which moves arrival
           times and therefore the frequencies of multipath cancellations --
           real, and worth recording. It is **not** a validity cliff at some
           particular number of degrees, and it is nowhere near the largest
           term. Before the token existed, this check gated on the weakest
           environmental variable and ignored every stronger one, so an
           acoustic comparison could pass while somebody had moved the
           microphone five centimetres -- a sizeable fraction of a wavelength
           at 3.5 kHz. That was false confidence, which is worse than no check.
        """
        return self.why_incomparable(other, temp_tolerance_c) is None

    def self_comparable(self, temp_tolerance_c: float = 2.0) -> bool:
        """Whether a measurement taken under these conditions can be compared
        **to another taken under exactly the same ones**.

        Comparing a provenance against itself looks vacuous and is not. Every
        pairwise term drops out, leaving only the *structural* requirements --
        an acoustic measurement needs a setup token and a temperature. If
        those are missing, no verdict is reachable no matter what is measured
        next, and the run can be stopped before it fits or writes anything
        rather than discovering it at the verification stage, after the
        device has already been changed.
        """
        return self.why_incomparable(self, temp_tolerance_c) is None


@dataclass(frozen=True)
class Measurement:
    """One impulse response and everything needed to interpret it.

    **Index convention.** ``impulse`` is aligned so that sample 0 is the
    timing-reference instant:

    * With a hardware loopback, sample 0 is when the reference arrived, so the
      interface's own round-trip latency is already removed. The acoustic
      arrival's index within ``impulse`` is therefore *equal to* the
      propagation delay -- ``arrival_samples`` means both things at once, and
      cannot drift apart.
    * Without a loopback, sample 0 is the deconvolution's zero-delay point,
      which still contains unknown interface latency. That is precisely why
      delay and phase are unavailable in that case.

    Attributes:
        impulse: Real-valued float64 IR, aligned as above.
        provenance: Conditions of capture.
        arrival_samples: Arrival index into ``impulse``. Under a loopback
            that is also the propagation delay; under an acoustic reference it
            is the propagation delay **minus the reference path**, which is an
            unmeasured constant. None when there was no reference at all.
        timing: What established t=0. See :class:`TimingReference` -- three
            states, because an acoustic reference gives relative timing and
            not absolute, and collapsing that into the loopback case would let
            absolute-delay figures out under a flag designed to stop them.
    """

    impulse: np.ndarray
    provenance: Provenance
    arrival_samples: int | None = None
    timing: TimingReference = TimingReference.NONE
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def has_timing_reference(self) -> bool:
        """Whether *some* reference established t=0. Derived, never stored.

        Kept because most callers only need to know that a time origin
        exists. Anything that reports an absolute delay must ask
        :attr:`timing` instead -- this property is true for an acoustic
        reference, which cannot support one.
        """
        return self.timing.gives_relative_delay

    def magnitude_dbfs(self, freqs_hz: np.ndarray) -> np.ndarray:
        """Magnitude response at ``freqs_hz``. Always available."""
        from . import metrics  # deferred: metrics imports this module

        return metrics.magnitude_db(
            self.impulse, self.provenance.sample_rate_hz, freqs_hz
        )

    def phase_rad(self, freqs_hz: np.ndarray) -> np.ndarray:
        """Phase response in radians at ``freqs_hz``.

        Raises:
            NoTimingReference: If no hardware loopback was captured. Without
                one, every phase curve carries an unknown linear term.
        """
        self._require_timing_reference("phase")
        from . import metrics  # deferred: metrics imports this module

        return metrics.phase_rad(self.impulse, self.provenance.sample_rate_hz, freqs_hz)

    def delay_samples(self) -> int:
        """**Absolute** propagation delay in samples. Loopback only.

        The one quantity an acoustic reference cannot give. Its reference
        speaker's own path length is an unmeasured constant sitting inside
        every arrival, so this number would be wrong by it -- silently, and by
        an amount that looks entirely plausible.

        For time alignment, use :meth:`relative_delay_samples`. That is what
        the optimizer actually needs: ``normalize_delays`` subtracts the
        common minimum anyway, so a shared unknown constant cancels.

        Raises:
            NoTimingReference: Unless a hardware loopback was captured.
        """
        self._require_timing_reference("delay")
        if not self.timing.gives_absolute_delay:
            raise NoTimingReference(
                f"absolute delay is unavailable: t=0 for this measurement was "
                f"set by {self.timing.value} reference, whose own path length "
                f"is an unmeasured constant inside every arrival. Relative "
                f"delay between measurements sharing that reference is "
                f"available from relative_delay_samples(); absolute delay is "
                f"not, and a number here would be wrong by a constant that "
                f"looks plausible."
            )
        assert self.arrival_samples is not None  # guaranteed by the check
        return self.arrival_samples

    def relative_delay_samples(self, other: Measurement) -> int:
        """Arrival difference against another measurement. Any reference.

        Valid under an acoustic reference *because* the unknown constant is
        common to both and subtracts out -- which is only true while the two
        share a geometry. Two measurements taken either side of the
        microphone moving have different constants and this number is then
        meaningless, so the provenance check is not optional here.

        Raises:
            NoTimingReference: If either measurement lacks a reference.
            IncomparableProvenance: If the two were not taken under
                comparable conditions -- which, for an acoustic reference,
                includes the operator's setup token.
        """
        self._require_timing_reference("relative delay")
        other._require_timing_reference("relative delay")  # noqa: SLF001
        if self.timing is not other.timing:
            raise NoTimingReference(
                f"these measurements were timed differently "
                f"({self.timing.value} and {other.timing.value}); their "
                f"arrivals are not on one time origin"
            )
        self.require_comparable(other)
        assert self.arrival_samples is not None
        assert other.arrival_samples is not None
        return self.arrival_samples - other.arrival_samples

    def group_delay_samples(self, freqs_hz: np.ndarray) -> np.ndarray:
        """Group delay in samples at ``freqs_hz``.

        The timing-reference rule names delay, phase **and group delay**, and
        group delay is derived from phase, so it inherits phase's unknown
        linear term as an unknown constant offset. It was reachable without a
        timing reference for longer than the other two because it lived only
        as a free function in :mod:`tuner.measure.metrics`, which takes a bare
        impulse array and so has no measurement to check.

        Raises:
            NoTimingReference: If no hardware loopback was captured.
        """
        self._require_timing_reference("group delay")
        from . import metrics  # deferred: metrics imports this module

        return metrics.group_delay_samples(
            self.impulse, self.provenance.sample_rate_hz, freqs_hz
        )

    def _require_timing_reference(self, quantity: str) -> None:
        if not self.has_timing_reference or self.arrival_samples is None:
            raise NoTimingReference(
                f"{quantity} is unavailable: this measurement has no timing "
                f"reference -- neither a hardware loopback nor an acoustic "
                f"one. Magnitude, RT60 and spatial averaging remain valid; "
                f"{quantity} does not."
            )

    def require_comparable(self, other: Measurement) -> None:
        """Raise unless ``other`` may be diffed against this measurement."""
        reason = self.provenance.why_incomparable(other.provenance)
        if reason is not None:
            raise IncomparableProvenance(
                f"measurements cannot be meaningfully compared: {reason}"
            )
