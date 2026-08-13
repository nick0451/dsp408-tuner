"""Tests for the stimulus safety limiter.

These guard rules whose failure mode is destroyed hardware, so they assert that
violations *raise* rather than warn-and-continue.
"""

from itertools import pairwise

import numpy as np
import pytest

from tuner import safety
from tuner.safety.limits import DEFAULT_CEILING_DBFS, START_LEVEL_DBFS, ChannelLimit


def sine(peak: float = 1.0, n: int = 1024) -> np.ndarray:
    return peak * np.sin(np.linspace(0, 20 * np.pi, n))


class TestChannelLimit:
    def test_uncharacterized_channel_defaults_to_conservative_ceiling(self):
        assert ChannelLimit().ceiling_dbfs == DEFAULT_CEILING_DBFS
        assert ChannelLimit().characterized is False

    def test_uncharacterized_channel_cannot_be_raised(self):
        # Rule 4: an unknown channel is treated as a tweeter.
        with pytest.raises(safety.SafetyViolation, match="uncharacterized"):
            ChannelLimit(ceiling_dbfs=-3.0, characterized=False)

    def test_characterized_channel_may_be_raised(self):
        limit = ChannelLimit(ceiling_dbfs=-6.0, characterized=True)
        assert limit.ceiling_dbfs == -6.0


class TestCeilingCheck:
    def test_level_above_ceiling_raises(self):
        with pytest.raises(safety.SafetyViolation, match="exceeds channel ceiling"):
            safety.check_ceiling(-6.0, ChannelLimit())

    def test_level_at_ceiling_is_allowed(self):
        safety.check_ceiling(DEFAULT_CEILING_DBFS, ChannelLimit())


class TestRamp:
    def test_ramp_starts_quiet(self):
        assert safety.ramp_levels_dbfs(-20.0)[0] == START_LEVEL_DBFS

    def test_ramp_reaches_target(self):
        assert safety.ramp_levels_dbfs(-20.0)[-1] == pytest.approx(-20.0)

    def test_ramp_is_monotonic(self):
        levels = safety.ramp_levels_dbfs(-20.0, steps=6)
        assert all(b > a for a, b in pairwise(levels))

    def test_target_below_start_is_single_step(self):
        assert safety.ramp_levels_dbfs(-40.0) == [-40.0]


class TestCaptureSanity:
    def test_clipping_raises(self):
        with pytest.raises(safety.SafetyViolation, match="clipping"):
            safety.assert_capture_sane(sine(peak=1.0))

    def test_dc_offset_raises(self):
        with pytest.raises(safety.SafetyViolation, match="DC offset"):
            safety.assert_capture_sane(sine(peak=0.1) + 0.05)

    def test_clean_capture_passes(self):
        safety.assert_capture_sane(sine(peak=0.5))

    def test_channel_number_appears_in_message(self):
        with pytest.raises(safety.SafetyViolation, match="channel 3"):
            safety.assert_capture_sane(sine(peak=1.0), channel=3)


class TestApply:
    def test_scales_to_requested_level(self):
        out = safety.apply(sine(), -30.0, ChannelLimit())
        assert 20 * np.log10(np.max(np.abs(out))) == pytest.approx(-30.0)

    def test_refuses_level_above_ceiling(self):
        with pytest.raises(safety.SafetyViolation):
            safety.apply(sine(), 0.0, ChannelLimit())

    def test_normalizes_regardless_of_input_peak(self):
        quiet = safety.apply(sine(peak=0.01), -30.0, ChannelLimit())
        loud = safety.apply(sine(peak=0.9), -30.0, ChannelLimit())
        assert np.max(np.abs(quiet)) == pytest.approx(np.max(np.abs(loud)))

    def test_silent_input_stays_silent(self):
        out = safety.apply(np.zeros(64), -30.0, ChannelLimit())
        assert np.all(out == 0.0)


class TestCeilingFromDeviceState:
    """Hard safety rule 6, as code rather than as operator arithmetic.

    The limiter caps what we transmit; the driver gets that plus the device's
    channel gain and EQ boost, and nothing in `tuner.safety` can see either.
    That correction was done by hand every time, from memory. The closed loop
    is where that stops working: the optimizer writes a boost and then
    immediately sweeps the channel it just boosted, with no human in between.
    """

    def test_no_device_gain_leaves_the_ceiling_alone(self):
        limit = safety.ceiling_for_device_state(0.0, ())
        assert limit.ceiling_dbfs == safety.DEFAULT_CEILING_DBFS

    def test_an_unraised_ceiling_is_not_characterized(self):
        # The flag means "somebody knows what is connected", and deriving a
        # ceiling from device state is not that. Reporting True here would
        # relax nothing -- the guard only fires above the default -- but it
        # would put "characterized" next to a channel rule 4 says to treat as
        # a tweeter, which is the wrong direction to be wrong in.
        assert safety.ceiling_for_device_state(0.0, ()).characterized is False
        assert safety.ceiling_for_device_state(6.0, (12.0,)).characterized is False

    def test_channel_gain_is_subtracted(self):
        limit = safety.ceiling_for_device_state(6.0, ())
        assert limit.ceiling_dbfs == safety.DEFAULT_CEILING_DBFS - 6.0

    def test_eq_boost_is_subtracted(self):
        limit = safety.ceiling_for_device_state(0.0, (12.0,))
        assert limit.ceiling_dbfs == safety.DEFAULT_CEILING_DBFS - 12.0

    def test_the_documented_disaster_case(self):
        # CLAUDE.md rule 6, verbatim: "A +12 dB band and a +6 dB channel gain
        # turn a -18 dBFS stimulus into 0 dBFS at the speaker."
        limit = safety.ceiling_for_device_state(6.0, (12.0,))
        assert limit.ceiling_dbfs == safety.DEFAULT_CEILING_DBFS - 18.0
        # And the stimulus that would have reached full scale is now refused.
        with pytest.raises(safety.SafetyViolation):
            safety.check_ceiling(-18.0, limit)

    def test_boosts_sum_rather_than_maximise(self):
        # Overlapping bands add. Assuming they all peak together is
        # pessimistic; the cost of pessimism is a quiet sweep, the cost of
        # optimism is a tweeter.
        limit = safety.ceiling_for_device_state(0.0, (6.0, 4.0, 2.0))
        assert limit.ceiling_dbfs == safety.DEFAULT_CEILING_DBFS - 12.0

    def test_cuts_do_not_raise_the_ceiling(self):
        # A -12 dB band does not license a 12 dB hotter stimulus: it attenuates
        # one narrow region while the rest of the sweep passes at full level.
        limit = safety.ceiling_for_device_state(0.0, (-12.0, -6.0))
        assert limit.ceiling_dbfs == safety.DEFAULT_CEILING_DBFS

    def test_negative_channel_gain_does_not_raise_it_either(self):
        limit = safety.ceiling_for_device_state(-20.0, ())
        assert limit.ceiling_dbfs == safety.DEFAULT_CEILING_DBFS

    def test_it_cannot_be_used_to_exceed_the_default_ceiling(self):
        # The guard in ChannelLimit must still bite: this helper computes a
        # ceiling, it does not grant permission to raise one.
        with pytest.raises(safety.SafetyViolation):
            safety.ChannelLimit(ceiling_dbfs=-6.0, characterized=False)

    def test_a_characterized_driver_ceiling_is_honoured(self):
        limit = safety.ceiling_for_device_state(6.0, (12.0,), driver_ceiling_dbfs=-6.0)
        assert limit.ceiling_dbfs == -24.0
        assert limit.characterized


class TestTheCaptureLevelReport:
    """Setting input gain is otherwise guesswork, and guessing costs retries.

    In a car a retry costs a seat position, so the numbers an operator needs
    to set gain once are worth reporting rather than leaving them to be
    inferred from whether the sweep survived.
    """

    def test_headroom_is_reported_for_a_clean_capture(self):
        from tuner.safety.limits import inspect_capture

        quiet = 0.1 * np.sin(np.linspace(0, 100, 4096))
        level = inspect_capture(quiet)
        assert level.peak_dbfs == pytest.approx(-20.0, abs=0.1)
        assert level.headroom_db == pytest.approx(20.0, abs=0.1)
        assert not level.clipped

    def test_an_empty_capture_does_not_divide_by_zero(self):
        from tuner.safety.limits import inspect_capture

        level = inspect_capture(np.array([]))
        assert level.n_samples == 0
        assert level.fraction_at_ceiling == 0.0
        assert not level.clipped

    def test_it_counts_how_much_of_the_capture_is_at_full_scale(self):
        from tuner.safety.limits import inspect_capture

        buffer = np.full(1000, 0.5)
        buffer[:37] = 1.0
        level = inspect_capture(buffer)
        assert level.samples_at_ceiling == 37
        assert level.fraction_at_ceiling == pytest.approx(0.037)


class TestTheClippingDiagnosis:
    """A bare peak said it happened and nothing about what to do."""

    def _clip(self, n_clipped: int, total: int = 10_000) -> np.ndarray:
        buffer = np.full(total, 0.3)
        buffer[:n_clipped] = 1.0
        return buffer

    def test_a_few_samples_reads_as_a_transient(self):
        # Worth simply repeating the sweep -- a connector nudged, a door shut.
        from tuner.safety.limits import SafetyViolation, assert_capture_sane

        with pytest.raises(SafetyViolation, match="transient"):
            assert_capture_sane(self._clip(3))

    def test_sustained_clipping_says_repeating_will_not_help(self):
        # A chain running hot fails the same way every time, and telling the
        # operator to try again would waste the session.
        from tuner.safety.limits import SafetyViolation, assert_capture_sane

        with pytest.raises(SafetyViolation, match="running hot"):
            assert_capture_sane(self._clip(900))

    def test_the_message_names_the_knob(self):
        # For a clipped capture that is the interface input gain. Lowering the
        # stimulus would buy headroom by giving away signal-to-noise, and it
        # is the wrong instinct precisely because it also "works".
        from tuner.safety.limits import SafetyViolation, assert_capture_sane

        with pytest.raises(SafetyViolation, match="input gain"):
            assert_capture_sane(self._clip(900))

    def test_the_message_carries_the_numbers_to_act_on(self):
        from tuner.safety.limits import SafetyViolation, assert_capture_sane

        with pytest.raises(SafetyViolation) as excinfo:
            assert_capture_sane(self._clip(900), channel=2)
        text = str(excinfo.value)
        assert "channel 2" in text
        assert "dBFS" in text
        assert "900 of 10000" in text

    def test_a_clean_capture_still_passes(self):
        # Vacuity: the diagnosis must not fire on an ordinary measurement.
        from tuner.safety.limits import assert_capture_sane

        assert_capture_sane(0.4 * np.sin(np.linspace(0, 500, 8192)))

    def test_dc_offset_suggests_where_to_look(self):
        from tuner.safety.limits import SafetyViolation, assert_capture_sane

        with pytest.raises(SafetyViolation, match="phantom-power"):
            assert_capture_sane(np.full(1000, 0.5))
