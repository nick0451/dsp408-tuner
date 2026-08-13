"""M4 -- the closed tuning loop.

Every part of a tune already existed and was tested in isolation: ``biquad``
fits, ``target`` supplies the curve and the level/shape split, ``delay``
aligns, ``budget`` bounds, ``verify`` accepts or rejects, ``snapshot`` rolls
back two independent ways. **Nothing joined them into a run.** This package is
that join.

Its job is not arithmetic -- the parts do that. Its job is the four things
none of them can do alone, each of which is a way the improvement invariant
gets satisfied without being met:

1. **Freeze the objective before the run.** Chosen after seeing results,
   "improved" is trivially satisfiable by re-weighting and every individual
   step still looks honest. :class:`~tuner.orchestrate.objective.
   MagnitudeObjective` is fingerprinted at plan time and the fingerprint is
   re-checked before the verdict, so the freeze is enforced rather than
   asserted.
2. **Measure the repeatability floor this session.** It moves with
   temperature, mounting and ambient noise, so it is never inherited.
3. **Three outcomes.** Incomparable provenance is *indeterminate*, and
   indeterminate rolls back.
4. **Roll back and prove it acoustically.** A restore verified only by
   readback proves the bytes went back, not that the system did.

And one the operator already does by hand, which becomes load-bearing once
nobody is watching: **isolate one driver, and prove the isolation by
measurement.** See :mod:`tuner.orchestrate.isolate`.

Two structural rules the run obeys, both from CLAUDE.md:

* **The device is never inside the optimizer's inner loop.** Fit offline,
  write once per round. Every write is immediately non-volatile.
* **Nothing sweeps without a ceiling derived from the device's own state.**
  The limiter cannot see channel gain or EQ boost; the run reads them back and
  subtracts them before every sweep.

Note what is **not** re-exported here: :class:`~tuner.orchestrate.rig.
AcousticMeasurer`. Importing it pulls in the audio layer and hence PortAudio,
and the run's whole point is that it can be rehearsed without either. Import
it from ``tuner.orchestrate.rig`` when you actually have a rig.
"""

from .isolate import IsolationError, Isolator, MuteIsolator, NoIsolation
from .objective import MagnitudeObjective, ObjectiveChanged
from .plan import PlanError, TunePlan
from .run import (
    Measurer,
    OrchestrationError,
    RollbackFailed,
    RunReport,
    Stage,
    StageRecord,
    TuneRun,
)

__all__ = [
    "IsolationError",
    "Isolator",
    "MagnitudeObjective",
    "Measurer",
    "MuteIsolator",
    "NoIsolation",
    "ObjectiveChanged",
    "OrchestrationError",
    "PlanError",
    "RollbackFailed",
    "RunReport",
    "Stage",
    "StageRecord",
    "TuneRun",
    "TunePlan",
]
