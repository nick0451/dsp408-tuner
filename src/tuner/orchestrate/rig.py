"""The real :class:`~tuner.orchestrate.run.Measurer`: sweeps through a rig.

**Proven on hardware 2026-08-12, through a cable.** It drove the first
end-to-end closed loop -- arm, floor, baseline, fit, write, verify, settle --
against the real DSP-408 with OUT1 patched straight into the interface. So the
sweep-per-output plumbing is no longer hypothetical. **It has still never met
a microphone**: nothing here has been exercised with air, a loudspeaker or a
listening position in the path, and ``move_microphone`` has never been called
by anything but a test.

What this adapter is, and is not
--------------------------------
It is the glue between :func:`tuner.measure.capture.capture_sweep` and the
run: one sweep per output per listening position, at the ceiling the run
computed, with the session's linearity checked once at the start.

**Channel isolation is not here.** It belongs to
:mod:`tuner.orchestrate.isolate`, which the run calls before each sweep,
because isolation is a sequence of device writes and therefore has to be
armed, journalled, kept out of the tune's diff manifest, and restored at the
end. A measurer that muted channels behind the run's back would put mute
states into the tune the run then wrote to the device.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from ..measure.capture import CaptureConfig, SessionInfo, capture_sweep
from ..measure.qa import LinearityResult, require_linear_path
from ..measure.result import Coupling, Measurement
from ..safety.limits import ChannelLimit


class RigError(RuntimeError):
    """The rig cannot honour what the run asked for."""


@dataclass
class AcousticMeasurer:
    """Sweeps one DSP output at each listening position.

    Attributes:
        config: Template capture configuration. ``limit`` and ``level_dbfs``
            are **overridden per sweep** from the ceiling the run derived from
            live device state -- a caller's values for those two are ignored
            rather than merged, because merging would let a stale template
            raise a ceiling the run had just lowered.
        session: Provenance for every measurement taken.
        positions: Names of the listening positions, in the objective's
            weight order. Length must match the objective.
        move_microphone: ``move_microphone(position_name)`` blocks until the
            microphone is in place. The default raises for more than one
            position, so a multi-seat run cannot silently measure one seat
            four times.
        linearity: A level-linearity result for this session's path, measured
            with tones inside the channels' passbands. Checked once, on the
            first sweep. ``None`` means the run is **not verified**, and this
            class says so out loud rather than proceeding quietly.
        headroom_db: How far below the ceiling to sweep. Zero sweeps at the
            ceiling exactly, which is permitted and leaves nothing for a
            driver that is more efficient than expected.
    """

    config: CaptureConfig
    session: SessionInfo
    positions: tuple[str, ...]
    move_microphone: Callable[[str], None] | None = None
    linearity: LinearityResult | None = None
    headroom_db: float = 3.0
    _linearity_checked: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not self.positions:
            raise RigError("a rig needs at least one listening position")
        if self.headroom_db < 0:
            raise RigError(
                f"headroom {self.headroom_db} dB is negative, which would sweep "
                f"above the ceiling the run computed"
            )
        if len(self.positions) > 1 and self.move_microphone is None:
            raise RigError(
                f"{len(self.positions)} listening positions but no way to ask "
                f"for the microphone to be moved; without one this would "
                f"measure the same seat {len(self.positions)} times and the "
                f"objective would report a multi-seat compromise that was "
                f"never measured"
            )
        if self.session.coupling is Coupling.ACOUSTIC and not self.session.setup_token:
            # Checked here, at construction, and not left to the verification
            # stage. An acoustic session with no setup token can never produce
            # a verdict -- the baseline and the re-measurement are structurally
            # incomparable however well the tune goes -- and discovering that
            # at VERIFY means discovering it after the fit and after the write.
            # That is not hypothetical: it is exactly how the missing
            # temperature reading surfaced on 2026-08-12.
            raise RigError(
                "an acoustic session needs a setup token: the operator's "
                "claim, in their own words, of what the physical "
                "configuration is -- microphone position, seat, doors, "
                "windows, HVAC, occupancy. None of it is checkable by code "
                "and all of it moves the response more than temperature "
                "does. Without one no acoustic comparison is possible, so "
                "the run would fit, write, and only then report "
                "indeterminate. Pass SessionInfo(setup_token=...), or "
                "declare Coupling.ELECTRICAL if nothing is going through air."
            )

    def measure(
        self, output: int, limit: ChannelLimit, tag: str
    ) -> Sequence[Measurement]:
        self._check_linearity_once()
        results: list[Measurement] = []
        for position in self.positions:
            if self.move_microphone is not None:
                self.move_microphone(position)
            config = replace(
                self.config,
                limit=limit,
                level_dbfs=limit.ceiling_dbfs - self.headroom_db,
            )
            notes = dict(self.session.notes)
            notes.update({"position": position, "dsp_output": str(output), "tag": tag})
            captured = capture_sweep(config, replace(self.session, notes=notes))
            results.append(self._one(captured, output, position))
        return results

    def _one(
        self, captured: dict[int, Measurement], output: int, position: str
    ) -> Measurement:
        """Exactly one measurement channel, or an error naming the ambiguity."""
        if len(captured) != 1:
            raise RigError(
                f"output {output} at {position!r} returned "
                f"{len(captured)} measurement channels ({sorted(captured)}); "
                f"the run scores one microphone per position and picking one "
                f"here would be a guess. Configure a single input channel."
            )
        return next(iter(captured.values()))

    def _check_linearity_once(self) -> None:
        """Level-linearity, checked once per session rather than per sweep.

        It costs ~14 s, which is too slow per sweep and trivial per run. The
        check is not optional politeness: a compressor, limiter, noise gate or
        AGC anywhere in the chain invalidates single-level measurements
        entirely, and Windows speech noise-suppression once manufactured a
        convincing 70 dB/octave rolloff on this very rig.

        With no result supplied this raises. The alternative -- proceeding
        with a warning -- produces a run whose every number is unverified and
        whose report does not say so.
        """
        if self._linearity_checked:
            return
        if self.linearity is None:
            raise RigError(
                "no level-linearity result was supplied, so this run would be "
                "unverified. Measure one with "
                "tuner.measure.qa.measure_level_linearity, using tones inside "
                "the passband of the channels being tuned, and pass it as "
                "AcousticMeasurer(linearity=...). A compressor or AGC anywhere "
                "in the chain invalidates every single-level measurement, and "
                "the resulting curves look entirely reasonable."
            )
        require_linear_path(self.linearity)
        self._linearity_checked = True
