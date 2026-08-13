"""Per-driver time alignment.

Two things are load-bearing here and both are tested for what they refuse as
much as what they compute:

* the timing-reference rule -- alignment from measurements without a loopback
  is arbitrary, and must raise rather than return a plausible number;
* the multi-position compromise -- **no delay aligns a driver at two seats at
  once**, so the honest output is a weighted compromise plus a reported
  residual, not a number that looks exact.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from tuner.measure.result import Measurement, NoTimingReference, Provenance
from tuner.measure.timing import TimingReference
from tuner.optimize.delay import (
    AlignmentError,
    align,
    relative_delay_samples,
    residual_spread_samples,
)

SR = 44_100


def measurement(arrival: int, reference: bool = True) -> Measurement:
    impulse = np.zeros(4096)
    impulse[arrival] = 1.0
    return Measurement(
        impulse=impulse,
        provenance=Provenance(
            device="synthetic",
            sample_rate_hz=SR,
            gains_db=(0.0,),
            timestamp=datetime(2026, 1, 1, 12, 0),
            temperature_c=20.0,
            setup_token="synthetic, unmoved",
        ),
        arrival_samples=arrival,
        timing=(TimingReference.LOOPBACK if reference else TimingReference.NONE),
    )


class TestTimingReferenceRule:
    """Without a loopback every arrival carries an unknown constant."""

    def test_relative_delay_refuses_without_a_reference(self):
        with pytest.raises(NoTimingReference):
            relative_delay_samples(measurement(300, reference=False), measurement(340))

    def test_it_checks_both_measurements(self):
        with pytest.raises(NoTimingReference):
            relative_delay_samples(measurement(300), measurement(340, reference=False))

    def test_align_refuses_if_any_measurement_lacks_one(self):
        with pytest.raises(NoTimingReference):
            align({0: [measurement(300)], 1: [measurement(340, reference=False)]})

    def test_the_rule_is_enforced_by_the_accessor_not_a_copy(self):
        # If Measurement's guard were ever removed, this module must not have
        # its own duplicate quietly keeping the tests green.
        m = measurement(300, reference=False)
        with pytest.raises(NoTimingReference):
            m.delay_samples()


class TestRelativeDelay:
    def test_a_later_driver_reports_positive(self):
        assert relative_delay_samples(measurement(340), measurement(300)) == 40

    def test_an_earlier_driver_reports_negative(self):
        assert relative_delay_samples(measurement(300), measurement(340)) == -40

    def test_identical_arrivals_report_zero(self):
        assert relative_delay_samples(measurement(300), measurement(300)) == 0

    def test_it_is_antisymmetric(self):
        a, b = measurement(311), measurement(407)
        assert relative_delay_samples(a, b) == -relative_delay_samples(b, a)


class TestSinglePosition:
    """One seat has an exact answer: delay everything to the latest arrival."""

    def test_all_drivers_end_up_aligned(self):
        drivers = {
            0: [measurement(300)],
            1: [measurement(340)],
            2: [measurement(312)],
            3: [measurement(355)],
        }
        delays = align(drivers)
        assert delays == {0: 55, 1: 15, 2: 43, 3: 0}
        assert residual_spread_samples(drivers, delays) == [0.0]

    def test_the_latest_driver_gets_no_delay(self):
        drivers = {0: [measurement(300)], 1: [measurement(500)]}
        delays = align(drivers)
        assert delays[1] == 0
        assert delays[0] == 200

    def test_delays_are_non_negative_and_minimum_zero(self):
        drivers = {i: [measurement(300 + 17 * i)] for i in range(8)}
        delays = align(drivers)
        assert min(delays.values()) == 0
        assert all(d >= 0 for d in delays.values())

    def test_already_aligned_drivers_need_nothing(self):
        drivers = {0: [measurement(300)], 1: [measurement(300)]}
        assert align(drivers) == {0: 0, 1: 0}

    def test_a_single_driver_is_trivially_aligned(self):
        assert align({3: [measurement(412)]}) == {3: 0}

    def test_absolute_offset_does_not_change_the_answer(self):
        # Only relative delay matters, and shared delay RAM makes the absolute
        # values expensive.
        near = {0: [measurement(300)], 1: [measurement(340)]}
        far = {0: [measurement(1300)], 1: [measurement(1340)]}
        assert align(near) == align(far)


class TestMultiPosition:
    def test_a_consistent_offset_is_corrected_exactly(self):
        # Driver 0 is 40 samples early at both seats, so one delay fixes both.
        drivers = {
            0: [measurement(300), measurement(500)],
            1: [measurement(340), measurement(540)],
        }
        delays = align(drivers)
        assert delays == {0: 40, 1: 0}
        assert residual_spread_samples(drivers, delays) == [0.0, 0.0]

    def test_a_symmetric_conflict_gets_no_delay(self):
        # Driver 0 is 40 early at one seat and 40 late at the other. No shift
        # improves the pair, and the honest answer is to leave it alone --
        # not to pick a seat silently.
        drivers = {
            0: [measurement(300), measurement(360)],
            1: [measurement(340), measurement(320)],
        }
        delays = align(drivers)
        assert delays == {0: 0, 1: 0}
        assert residual_spread_samples(drivers, delays) == [40.0, 40.0]

    def test_weighting_trades_one_seat_for_another(self):
        drivers = {
            0: [measurement(300), measurement(360)],
            1: [measurement(340), measurement(320)],
        }
        driver_seat = align(drivers, position_weights=[0.9, 0.1])
        spread = residual_spread_samples(drivers, driver_seat)
        assert spread[0] < spread[1]
        assert driver_seat[0] > 0

    def test_the_weighting_is_symmetric(self):
        drivers = {
            0: [measurement(300), measurement(360)],
            1: [measurement(340), measurement(320)],
        }
        first = residual_spread_samples(drivers, align(drivers, [0.9, 0.1]))
        second = residual_spread_samples(drivers, align(drivers, [0.1, 0.9]))
        assert first[0] == pytest.approx(second[1])
        assert first[1] == pytest.approx(second[0])

    def test_uniform_weights_match_the_default(self):
        drivers = {
            0: [measurement(300), measurement(370)],
            1: [measurement(340), measurement(325)],
        }
        assert align(drivers) == align(drivers, [1.0, 1.0])
        assert align(drivers) == align(drivers, [5.0, 5.0])

    def test_it_minimises_the_weighted_spread(self):
        # Check the closed form against a brute-force search, so a future
        # rewrite cannot quietly produce a worse compromise.
        drivers = {
            0: [measurement(300), measurement(371)],
            1: [measurement(340), measurement(322)],
            2: [measurement(318), measurement(355)],
        }
        weights = np.array([0.7, 0.3])
        got = align(drivers, weights)

        arrivals = np.array(
            [[m.delay_samples() for m in drivers[o]] for o in sorted(drivers)],
            dtype=float,
        )

        def cost(delays):
            shifted = arrivals + np.asarray(delays)[:, None]
            centred = shifted - shifted.mean(axis=0, keepdims=True)
            return float((weights * (centred**2).sum(axis=0)).sum())

        best = cost([got[o] for o in sorted(drivers)])
        rng = np.random.default_rng(0)
        for _ in range(400):
            trial = [got[o] + int(rng.integers(-6, 7)) for o in sorted(drivers)]
            assert cost(trial) >= best - 1e-6


class TestResidualReporting:
    def test_it_reports_zero_when_alignment_is_exact(self):
        drivers = {0: [measurement(300)], 1: [measurement(340)]}
        assert residual_spread_samples(drivers, align(drivers)) == [0.0]

    def test_it_reports_what_the_compromise_cost(self):
        # The number that stops a multi-seat alignment being presented as if
        # it were exact.
        drivers = {
            0: [measurement(300), measurement(400)],
            1: [measurement(340), measurement(340)],
        }
        spread = residual_spread_samples(drivers, align(drivers))
        assert len(spread) == 2
        assert max(spread) > 0

    def test_a_missing_delay_is_an_error(self):
        drivers = {0: [measurement(300)], 1: [measurement(340)]}
        with pytest.raises(AlignmentError, match="no delay given"):
            residual_spread_samples(drivers, {0: 0})


class TestMalformedInput:
    def test_no_drivers(self):
        with pytest.raises(AlignmentError, match="no drivers"):
            align({})

    def test_no_measurements(self):
        with pytest.raises(AlignmentError, match="no measurements"):
            align({0: [], 1: []})

    def test_uneven_position_counts(self):
        with pytest.raises(AlignmentError, match="same positions"):
            align({0: [measurement(300)], 1: [measurement(340), measurement(350)]})

    def test_wrong_number_of_weights(self):
        with pytest.raises(AlignmentError, match="expected 2"):
            align(
                {0: [measurement(300), measurement(360)]},
                position_weights=[1.0, 1.0, 1.0],
            )

    def test_negative_weights(self):
        with pytest.raises(AlignmentError, match="not be negative"):
            align({0: [measurement(300), measurement(360)]}, [1.0, -1.0])

    def test_weights_summing_to_zero(self):
        with pytest.raises(AlignmentError, match="sum to zero"):
            align({0: [measurement(300), measurement(360)]}, [0.0, 0.0])


class TestBudgetInteraction:
    def test_normalised_delays_free_shared_ram(self):
        from tuner.dsp.backend import ChannelConfig
        from tuner.optimize.budget import normalize_delays

        channels = [ChannelConfig(delay_samples=d) for d in (1200, 1240, 1215)]
        normalised = normalize_delays(channels)
        assert [c.delay_samples for c in normalised] == [0, 40, 15]

        # Acoustically identical -- every difference preserved.
        before = [c.delay_samples for c in channels]
        after = [c.delay_samples for c in normalised]
        assert np.allclose(np.diff(before), np.diff(after))

    def test_already_normalised_is_returned_unchanged(self):
        from tuner.dsp.backend import ChannelConfig
        from tuner.optimize.budget import normalize_delays

        channels = [ChannelConfig(delay_samples=d) for d in (0, 40, 15)]
        assert [c.delay_samples for c in normalize_delays(channels)] == [0, 40, 15]

    def test_an_empty_list_is_fine(self):
        from tuner.optimize.budget import normalize_delays

        assert normalize_delays([]) == []

    def test_a_negative_delay_is_rejected(self):
        # A delay line cannot run backwards. If alignment produced this, the
        # alignment is wrong rather than merely unnormalised.
        from tuner.dsp.backend import ChannelConfig
        from tuner.optimize.budget import normalize_delays

        with pytest.raises(ValueError, match="negative"):
            normalize_delays([ChannelConfig(delay_samples=-5)])

    def test_alignment_output_is_already_normalised(self):
        from tuner.dsp.backend import ChannelConfig
        from tuner.optimize.budget import normalize_delays

        drivers = {i: [measurement(300 + 11 * i)] for i in range(4)}
        delays = align(drivers)
        channels = [ChannelConfig(delay_samples=delays[i]) for i in sorted(delays)]
        assert normalize_delays(channels) == channels
