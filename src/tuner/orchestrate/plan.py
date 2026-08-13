"""Everything a tuning run must decide **before** it measures anything.

A plan is the run's contract with itself. Once constructed it is frozen and
fingerprinted, and the run refuses to produce a verdict if either the plan or
its objective has moved. The point is not tidiness: the improvement invariant
is only meaningful if the thing being improved was chosen in advance.

Three decisions are here rather than defaulted, each because a default would
be wrong in a way that is hard to see afterwards.

**The scratch preset slot has no default.** There are six slots, every one may
hold an operator asset, storing to an occupied one destroys it with no undo,
and slot 1 is known to hold the original tune. The obvious default -- the
highest number, on the assumption that high slots are spare -- is exactly the
assumption that "fifteen slots" turned out to be. So the slot is required, the
operator's confirmation is required with it, and both go into provenance.

**A raised channel ceiling requires its basis in writing.** Hard safety rule 4
says a channel whose crossover is unknown is a tweeter and gets the lowest
ceiling, and that raising one is a deliberate act requiring knowledge of what
is connected. :class:`DriverCeiling` makes "deliberate" mean "typed a reason",
which is the difference between a decision and a keyword argument.

**Delay alignment is opt-in.** It needs a hardware loopback, and the rig that
validated the measurement engine does not have one. Defaulting it on would
produce a run that raises halfway through on every rig we currently own.

**Gang membership is declared, not read from the device.** Outputs 7 and 8 on
this system drive two subwoofers in one ported box, so they are one acoustic
source to measure and one correction to write, and mismatching them is a
mechanical hazard rather than a tonal choice. The device has a
``linkgroup_num`` field that says so -- and in 14 of the 40 ``.DDP`` backups it
reads 0 for that pair, because the vendor app wrote unlinking and never wrote
re-linking. A flag that is absent precisely when it matters cannot be the
source of truth for a constraint whose violation breaks a driver. So the gang
is declared here with a basis, and the device's flag becomes a cross-check
that warns when it disagrees.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..dsp.protocol import PRESET_SLOT_MAX, PRESET_SLOT_WORKING_AREA
from ..optimize.biquad import DEFAULT_CONSTRAINTS, FitConstraints
from ..safety.limits import DEFAULT_CEILING_DBFS
from .objective import MagnitudeObjective

#: Outputs on a DSP-408.
N_OUTPUTS = 8


class PlanError(ValueError):
    """The plan is malformed or unsafe. Raised at construction, never later."""


@dataclass(frozen=True)
class DriverCeiling:
    """A per-output stimulus ceiling and the knowledge that licenses it.

    ``basis`` is required and must say what is connected -- "SB Acoustics
    SB29RDNC tweeter, 4th-order high-pass at 3.5 kHz, 90 W" rather than
    "measured fine last time". It is recorded in the run's provenance, so a
    ceiling that turns out to be wrong can be traced to the claim that set it.
    """

    ceiling_dbfs: float
    basis: str

    def __post_init__(self) -> None:
        if not self.basis or not self.basis.strip():
            raise PlanError(
                "a driver ceiling needs a basis naming what is connected; "
                "hard safety rule 4 makes raising a ceiling a deliberate act, "
                "and an unexplained number is not one"
            )
        if self.ceiling_dbfs > 0.0:
            raise PlanError(f"ceiling {self.ceiling_dbfs} dBFS is above full scale")


@dataclass(frozen=True)
class Gang:
    """Outputs that must be measured together and corrected identically.

    The case that forced this into existence: two subwoofers in one ported
    box. They share an air load, so driving them unequally makes one take more
    than its share of the excursion while the other is back-driven -- and
    below port tuning, where the cone is barely loaded, that is how a driver
    fails. Matching them is mechanical, not tonal.

    It also means they are **one thing to measure**. Two drivers in one
    enclosure, low-passed at 55 Hz, radiate as a single source at the
    listening position; there is nothing to separate and no reason to try.

    Attributes:
        outputs: Members, at least one. The lowest is the gang's
            :attr:`leader`, which is how the objective and the measurement
            dictionaries name it.
        basis: Why these move together. Required for a gang of more than one,
            because that is a claim about the physical system that no code can
            check -- the same reason :class:`DriverCeiling` needs one.
            Recorded in provenance.
        name: Optional label for reports, e.g. ``"subs"``.
    """

    outputs: tuple[int, ...]
    basis: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        outputs = tuple(sorted(set(int(o) for o in self.outputs)))
        if not outputs:
            raise PlanError("a gang needs at least one output")
        for output in outputs:
            if not 0 <= output < N_OUTPUTS:
                raise PlanError(f"output {output} outside 0..{N_OUTPUTS - 1}")
        if len(outputs) > 1 and not self.basis.strip():
            raise PlanError(
                f"gang {list(outputs)} needs a basis saying why these outputs "
                f"move together -- a shared enclosure, a bridged pair, a "
                f"dipole. Ganging changes what gets measured and what gets "
                f"written, and unganging drivers that share an enclosure can "
                f"destroy them, so it is not a formatting choice."
            )
        object.__setattr__(self, "outputs", outputs)

    @property
    def leader(self) -> int:
        """Lowest member. Names the gang everywhere a single index is wanted."""
        return self.outputs[0]

    @property
    def is_solo(self) -> bool:
        return len(self.outputs) == 1

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        return "+".join(str(o + 1) for o in self.outputs)

    def as_dict(self) -> dict[str, object]:
        return {
            "outputs": list(self.outputs),
            "leader": self.leader,
            "basis": self.basis,
            "name": self.name,
        }


@dataclass(frozen=True)
class TunePlan:
    """The frozen inputs to one tuning run.

    Attributes:
        session_id: Ties the run, its snapshot, its repeatability floor and
            its journal together. The floor is a per-session quantity and the
            run refuses to use one measured under a different id.
        objective: The scalar being optimized, already frozen.
        scratch_slot: Preset slot the baseline is stored to. Required; see the
            module docstring.
        scratch_slot_confirmed_by: Who confirmed the slot is expendable, and
            when. Free text, recorded verbatim.
        scratch_slot_holds: What the operator says is currently in that slot,
            recorded as the claim it is. ``None`` means "believed empty",
            which is a weaker claim than a name and is written down as such.
        gangs: Outputs that move as one. Any source the objective weights that
            is not covered by a declared gang becomes a solo gang implicitly,
            so an eight-single-driver system declares nothing.
        snapshot_path: Where the on-disk baseline goes. Writes cannot be armed
            without it, and its digest is re-verified before every write.
        constraints: Fit bounds. Band count should come from the backend's
            limits via :func:`tuner.optimize.biquad.constraints_for`.
        driver_ceilings: Per-output ceiling overrides. Outputs not listed get
            the conservative default, which is the correct answer for any
            channel whose crossover is unknown.
        floor_repeats: How many identical measurements establish the session's
            repeatability floor.
        align_delays: Whether to compute and write per-output delays. Requires
            a hardware loopback on every measurement.
        preserve_working_area: Refuse to start if the device's working area
            already differs from the on-disk snapshot -- i.e. something
            changed it behind our back between capture and arm.
        notes: Free-form provenance.
    """

    session_id: str
    objective: MagnitudeObjective
    scratch_slot: int
    scratch_slot_confirmed_by: str
    snapshot_path: Path
    scratch_slot_holds: str | None = None
    gangs: tuple[Gang, ...] = ()
    constraints: FitConstraints = DEFAULT_CONSTRAINTS
    driver_ceilings: Mapping[int, DriverCeiling] = field(default_factory=dict)
    floor_repeats: int = 3
    align_delays: bool = False
    preserve_working_area: bool = True
    notes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id or not self.session_id.strip():
            raise PlanError("a run needs a session id; it ties the floor to the run")

        if self.scratch_slot == PRESET_SLOT_WORKING_AREA:
            raise PlanError(
                f"slot {PRESET_SLOT_WORKING_AREA} is the live working area, not "
                f"storage; storing the baseline there would 'back it up' over "
                f"itself and leave no restore point at all"
            )
        if not 1 <= self.scratch_slot <= PRESET_SLOT_MAX:
            raise PlanError(
                f"scratch slot {self.scratch_slot} outside 1..{PRESET_SLOT_MAX}"
            )
        if not self.scratch_slot_confirmed_by.strip():
            raise PlanError(
                f"slot {self.scratch_slot} must be confirmed expendable by the "
                f"operator before the run stores to it -- storing over an "
                f"occupied slot destroys it and the device has no undo"
            )

        object.__setattr__(
            self, "gangs", tuple(sorted(self.gangs, key=lambda g: g.leader))
        )
        seen: dict[int, Gang] = {}
        for gang in self.gangs:
            for output in gang.outputs:
                if output in seen:
                    raise PlanError(
                        f"output {output} is in two gangs, {seen[output].outputs} "
                        f"and {gang.outputs}; it can only move with one of them"
                    )
                seen[output] = gang

        for source in self.objective.sources:
            if not 0 <= source < N_OUTPUTS:
                raise PlanError(f"source {source} outside 0..{N_OUTPUTS - 1}")
            gang = seen.get(source)
            if gang is not None and gang.leader != source:
                raise PlanError(
                    f"the objective weights source {source}, but that output "
                    f"is a follower in gang {list(gang.outputs)}. Weight the "
                    f"gang's leader, {gang.leader}, instead -- a gang is scored "
                    f"once, as one source."
                )

        # There is deliberately no separate "gang weighted twice" check. The
        # follower check above already catches it: weighting both members of a
        # gang necessarily weights a follower, and it says so more usefully
        # than a count would. A second branch here read as coverage and could
        # never execute.

        for output, ceiling in self.driver_ceilings.items():
            if not 0 <= output < N_OUTPUTS:
                raise PlanError(f"ceiling given for output {output}, which is not one")
            if not isinstance(ceiling, DriverCeiling):
                raise PlanError(
                    f"output {output}: driver ceilings are DriverCeiling objects, "
                    f"not bare numbers -- the basis is the point"
                )
        object.__setattr__(
            self, "driver_ceilings", dict(sorted(self.driver_ceilings.items()))
        )

        if self.floor_repeats < 2:
            raise PlanError(
                f"the repeatability floor needs at least 2 repeats, got "
                f"{self.floor_repeats}; with one there is no spread to measure "
                f"and the floor would be zero, which accepts noise"
            )

        object.__setattr__(self, "snapshot_path", Path(self.snapshot_path))
        object.__setattr__(self, "notes", dict(self.notes))

    # -- derived -----------------------------------------------------------

    @property
    def sources(self) -> tuple[int, ...]:
        """What the run measures and scores, by leader output.

        Taken from the objective, never listed separately: two lists that
        could disagree is one list too many, and a source tuned but unweighted
        would be changed without being judged.
        """
        return self.objective.sources

    @property
    def gang_for_source(self) -> dict[int, Gang]:
        """Every source's gang, with solo gangs filled in for the undeclared.

        A system of eight separate drivers declares no gangs at all and gets
        eight solo ones here, so the rest of the run has a single shape to
        handle rather than two.
        """
        declared = {gang.leader: gang for gang in self.gangs}
        return {
            source: declared.get(source, Gang(outputs=(source,)))
            for source in self.sources
        }

    def gang(self, source: int) -> Gang:
        gangs = self.gang_for_source
        if source not in gangs:
            raise PlanError(f"{source} is not a source this run tunes")
        return gangs[source]

    @property
    def outputs(self) -> tuple[int, ...]:
        """Every **physical** output the run will write, ascending.

        Distinct from :attr:`sources`: a two-driver gang is one source and two
        outputs. The budget, the write stage and the isolator all want this
        one -- a gang costs two channels of delay RAM, not one.
        """
        return tuple(
            sorted(o for gang in self.gang_for_source.values() for o in gang.outputs)
        )

    def ceiling_for(self, output: int) -> tuple[float, bool]:
        """``(ceiling_dbfs, characterized)`` for one output.

        Unlisted outputs get the conservative default and ``characterized``
        False, which is rule 4's answer for a channel whose crossover is
        unknown -- treat it as a tweeter.
        """
        override = self.driver_ceilings.get(output)
        if override is None:
            return DEFAULT_CEILING_DBFS, False
        return override.ceiling_dbfs, True

    def ceiling_for_source(self, source: int) -> tuple[float, bool]:
        """The ceiling for sweeping a source: **the lowest of its members**.

        A gang is swept as one, so the stimulus reaches every member. Taking
        the minimum means a gang is never louder than its most fragile driver
        would allow alone. ``characterized`` is True only if *every* member is,
        because one unknown driver in a gang makes the gang unknown.
        """
        members = self.gang(source).outputs
        ceilings = [self.ceiling_for(o) for o in members]
        return min(c for c, _ in ceilings), all(known for _, known in ceilings)

    # -- the freeze --------------------------------------------------------

    def as_dict(self) -> dict[str, object]:
        """Canonical, JSON-safe form, recorded in the run report."""
        return {
            "session_id": self.session_id,
            "objective": self.objective.as_dict(),
            "objective_fingerprint": self.objective.fingerprint(),
            "scratch_slot": self.scratch_slot,
            "scratch_slot_confirmed_by": self.scratch_slot_confirmed_by,
            "scratch_slot_holds": self.scratch_slot_holds,
            "gangs": [gang.as_dict() for gang in self.gangs],
            "snapshot_path": str(self.snapshot_path),
            "constraints": {
                "max_bands": self.constraints.max_bands,
                "max_boost_db": self.constraints.max_boost_db,
                "max_cut_db": self.constraints.max_cut_db,
                "min_q": self.constraints.min_q,
                "max_q": self.constraints.max_q,
                "freq_range_hz": list(self.constraints.freq_range_hz),
                "bandwidth_step_octaves": self.constraints.bandwidth_step_octaves,
                "freq_step_hz": self.constraints.freq_step_hz,
                "gain_step_db": self.constraints.gain_step_db,
                "max_iterations": self.constraints.max_iterations,
                "seed": self.constraints.seed,
            },
            "driver_ceilings": {
                str(k): {"ceiling_dbfs": v.ceiling_dbfs, "basis": v.basis}
                for k, v in self.driver_ceilings.items()
            },
            "floor_repeats": self.floor_repeats,
            "align_delays": self.align_delays,
            "preserve_working_area": self.preserve_working_area,
            "notes": dict(self.notes),
        }

    def fingerprint(self) -> str:
        blob = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
