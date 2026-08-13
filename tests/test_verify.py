"""Tests for the improvement invariant.

The cases that matter are the ones where a naive `after < before` check would
wrongly accept: improvement inside the noise floor, and comparison across
incomparable provenance.
"""

from datetime import datetime, timedelta

import numpy as np
import pytest

from tuner.measure.result import Measurement, Provenance
from tuner.optimize.verify import (
    Objective,
    Outcome,
    RepeatabilityFloor,
    measure_repeatability,
    verify,
)

OBJECTIVE = Objective(name="test", position_weights=(1.0,))
FLOOR = RepeatabilityFloor(value=0.5, n_repeats=3, session_id="s1")


def provenance(
    temperature_c: float = 20.0, gains=(10.0,), setup_token: str = "bench, unmoved"
) -> Provenance:
    return Provenance(
        device="test-interface",
        sample_rate_hz=48_000,
        gains_db=gains,
        timestamp=datetime(2026, 1, 1, 12, 0),
        cal_sha256="abc123",
        temperature_c=temperature_c,
        setup_token=setup_token,
    )


def measurement(**kwargs) -> Measurement:
    return Measurement(impulse=np.zeros(16), provenance=provenance(**kwargs))


class TestAcceptance:
    def test_improvement_beyond_floor_is_accepted(self):
        v = verify(measurement(), measurement(), 10.0, 8.0, OBJECTIVE, FLOOR)
        assert v.outcome is Outcome.ACCEPTED
        assert not v.requires_rollback

    def test_improvement_inside_floor_is_rejected(self):
        # The case a bare `after < before` check gets wrong: a real numeric
        # improvement that is indistinguishable from measurement noise.
        v = verify(measurement(), measurement(), 10.0, 9.7, OBJECTIVE, FLOOR)
        assert v.outcome is Outcome.REJECTED
        assert "noise" in v.reason

    def test_improvement_exactly_at_floor_is_rejected(self):
        v = verify(measurement(), measurement(), 10.0, 9.5, OBJECTIVE, FLOOR)
        assert v.outcome is Outcome.REJECTED

    def test_regression_is_rejected(self):
        v = verify(measurement(), measurement(), 10.0, 12.0, OBJECTIVE, FLOOR)
        assert v.outcome is Outcome.REJECTED
        assert v.requires_rollback

    def test_higher_is_better_objective(self):
        merit = Objective(name="m", position_weights=(1.0,), lower_is_better=False)
        assert (
            verify(measurement(), measurement(), 8.0, 10.0, merit, FLOOR).outcome
            is Outcome.ACCEPTED
        )
        assert (
            verify(measurement(), measurement(), 10.0, 8.0, merit, FLOOR).outcome
            is Outcome.REJECTED
        )


class TestIndeterminate:
    def test_temperature_drift_is_indeterminate_not_success(self):
        # A large apparent improvement across a temperature change says
        # nothing about the tune.
        v = verify(
            measurement(temperature_c=20.0),
            measurement(temperature_c=31.0),
            10.0,
            5.0,
            OBJECTIVE,
            FLOOR,
        )
        assert v.outcome is Outcome.INDETERMINATE
        assert v.requires_rollback

    def test_changed_gain_is_indeterminate(self):
        v = verify(
            measurement(gains=(10.0,)),
            measurement(gains=(16.0,)),
            10.0,
            5.0,
            OBJECTIVE,
            FLOOR,
        )
        assert v.outcome is Outcome.INDETERMINATE

    def test_small_temperature_drift_stays_comparable(self):
        v = verify(
            measurement(temperature_c=20.0),
            measurement(temperature_c=21.0),
            10.0,
            8.0,
            OBJECTIVE,
            FLOOR,
        )
        assert v.outcome is Outcome.ACCEPTED

    def test_provenance_is_checked_before_scores(self):
        # Even a catastrophic regression reads as indeterminate when the
        # comparison itself is invalid.
        v = verify(
            measurement(temperature_c=20.0),
            measurement(temperature_c=40.0),
            10.0,
            99.0,
            OBJECTIVE,
            FLOOR,
        )
        assert v.outcome is Outcome.INDETERMINATE


class TestRollback:
    @pytest.mark.parametrize(
        ("outcome_scores", "expected"),
        [((10.0, 8.0), False), ((10.0, 9.9), True), ((10.0, 20.0), True)],
    )
    def test_anything_but_acceptance_rolls_back(self, outcome_scores, expected):
        base, res = outcome_scores
        v = verify(measurement(), measurement(), base, res, OBJECTIVE, FLOOR)
        assert v.requires_rollback is expected


class TestRepeatabilityFloor:
    def test_derived_from_spread(self):
        floor = measure_repeatability([10.0, 10.4, 9.8], session_id="s1")
        assert floor.value == pytest.approx(0.6)
        assert floor.n_repeats == 3

    def test_single_measurement_rejected(self):
        with pytest.raises(ValueError, match="at least 2"):
            measure_repeatability([10.0], session_id="s1")

    def test_floor_requires_repeats(self):
        with pytest.raises(ValueError, match="at least 2"):
            RepeatabilityFloor(value=0.5, n_repeats=1, session_id="s1")

    def test_negative_floor_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            RepeatabilityFloor(value=-0.1, n_repeats=3, session_id="s1")


class TestVerdict:
    def test_delta_reports_raw_change(self):
        v = verify(measurement(), measurement(), 10.0, 8.0, OBJECTIVE, FLOOR)
        assert v.delta == pytest.approx(-2.0)

    def test_reason_is_populated(self):
        v = verify(measurement(), measurement(), 10.0, 8.0, OBJECTIVE, FLOOR)
        assert OBJECTIVE.name in v.reason


class TestStalenessGuard:
    def test_floor_carries_session_id(self):
        # The floor is per-session; the session_id exists so a carried-over
        # floor can be detected rather than silently reused.
        assert measure_repeatability([1.0, 1.2], session_id="s2").session_id == "s2"


def test_timestamps_do_not_affect_comparability():
    # Elapsed time alone is fine -- a tuning run takes time. It is the
    # conditions that must match, not the clock.
    early = Provenance(
        device="d",
        sample_rate_hz=48_000,
        gains_db=(10.0,),
        timestamp=datetime(2026, 1, 1, 12, 0),
        cal_sha256="x",
        temperature_c=20.0,
        setup_token="unmoved",
    )
    late = Provenance(
        device="d",
        sample_rate_hz=48_000,
        gains_db=(10.0,),
        timestamp=datetime(2026, 1, 1, 12, 0) + timedelta(hours=2),
        cal_sha256="x",
        temperature_c=20.0,
        setup_token="unmoved",
    )
    assert early.comparable_to(late)


class TestTheFloorsTimescale:
    """A floor without a timescale is not a floor.

    Found 2026-08-12, the first time the loop met hardware. Three sweeps back
    to back gave 0.003 dB; by the time the rollback was checked the score had
    moved 0.006 dB, so the run reported `RollbackFailed` on a device that was
    byte-identical to its snapshot. Nothing was wrong with the restore -- the
    rig had drifted over a window the floor never sampled.
    """

    def _floor(self, span_s):
        return measure_repeatability([1.0, 1.2], session_id="s", span_s=span_s)

    def test_a_floor_that_spanned_the_interval_covers_it(self):
        assert self._floor(300.0).covers(240.0)
        assert self._floor(300.0).covers(300.0)

    def test_a_floor_measured_over_thirty_seconds_does_not_cover_a_run(self):
        # The M4 case, in the units it happened in: 30 s of repeats judging a
        # four-minute comparison.
        assert not self._floor(30.0).covers(240.0)

    def test_a_shortfall_of_one_sweep_is_not_worth_reporting(self):
        """Equality is unreachable, so the check must not demand it.

        The verification sweep is the thing being judged, so it always falls
        after the last repeat that can be taken with the device still holding
        the baseline. A run's floor is therefore short by about one sweep no
        matter what, and a warning that fires every time is one nobody reads.
        """
        assert self._floor(230.0).covers(240.0)

    def test_the_fraction_is_a_reporting_heuristic_with_a_stated_value(self):
        # Not a measured quantity, and quoted as such wherever it appears.
        # Pinned so a change to it is a deliberate edit rather than a drift.
        assert self._floor(120.0).covers(240.0)
        assert not self._floor(119.0).covers(240.0)

    def test_the_span_defaults_to_zero_rather_than_being_assumed(self):
        # An unstated span is the honest default: it covers nothing, so any
        # caller that checks will notice rather than inheriting a claim
        # nobody made.
        assert measure_repeatability([1.0, 1.2], session_id="s").span_s == 0.0
        assert not measure_repeatability([1.0, 1.2], session_id="s").covers(1.0)

    def test_a_negative_span_is_refused(self):
        with pytest.raises(ValueError, match="span"):
            RepeatabilityFloor(value=0.1, n_repeats=2, session_id="s", span_s=-1.0)

    def test_the_span_does_not_change_the_floor_itself(self):
        # It qualifies the number; it does not adjust it. Scaling a floor by
        # elapsed time would need a model of how this rig's noise grows, and
        # no such measurement exists.
        assert self._floor(1.0).value == self._floor(1000.0).value
