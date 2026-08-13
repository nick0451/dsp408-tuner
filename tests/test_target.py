"""Target curves, and the level/shape split.

The split is the part worth testing hard. A target describes shape; absolute
level is a channel gain. Conflating them makes the fitter spend its whole band
budget on broadband boost and ask for gain the device may not have, and the
resulting tune looks like a mediocre fit rather than a units mistake.
"""

from __future__ import annotations

import numpy as np
import pytest

from tuner.optimize.target import (
    DEFAULT_LEVEL_BAND_HZ,
    TargetCurve,
    TargetError,
    correction_db,
    flat,
    from_points,
    harman_in_car,
    level_offset_db,
    tilted,
)

AXIS = np.geomspace(20.0, 20_000.0, 300)


class TestConstruction:
    def test_a_curve_holds_its_points(self):
        curve = from_points([(100, 3.0), (1000, 0.0), (10_000, -3.0)], name="x")
        assert curve.name == "x"
        assert curve.range_hz == (100.0, 10_000.0)

    def test_points_are_sorted_by_frequency(self):
        curve = from_points([(10_000, -3.0), (100, 3.0), (1000, 0.0)])
        assert list(curve.freqs_hz) == [100.0, 1000.0, 10_000.0]
        assert list(curve.magnitude_db) == [3.0, 0.0, -3.0]

    @pytest.mark.parametrize(
        ("freqs", "mags", "match"),
        [
            ([100.0], [0.0], "at least two"),
            ([100.0, 200.0], [0.0], "differ"),
            ([0.0, 200.0], [0.0, 0.0], "positive"),
            ([200.0, 100.0], [0.0, 0.0], "ascending"),
            ([100.0, 100.0], [0.0, 0.0], "ascending"),
            ([100.0, 200.0], [0.0, np.nan], "non-finite"),
        ],
    )
    def test_malformed_curves_are_rejected(self, freqs, mags, match):
        with pytest.raises(TargetError, match=match):
            TargetCurve(np.array(freqs), np.array(mags))

    def test_too_few_points_is_rejected(self):
        with pytest.raises(TargetError, match="at least two"):
            from_points([(100, 0.0)])


class TestInterpolation:
    def test_it_interpolates_in_log_frequency(self):
        # 316 Hz is the geometric midpoint of 100 and 1000, so a curve that
        # drops 6 dB across that decade must read -3 there. Linear-frequency
        # interpolation would give about -5.4.
        curve = from_points([(100, 0.0), (1000, -6.0)])
        assert curve.at(np.array([316.228]))[0] == pytest.approx(-3.0, abs=0.01)

    def test_it_reproduces_its_own_points(self):
        curve = from_points([(100, 3.0), (1000, 0.0), (10_000, -3.0)])
        got = curve.at(curve.freqs_hz)
        assert np.allclose(got, curve.magnitude_db)

    def test_it_clamps_rather_than_extrapolating(self):
        # A -3 dB/decade tilt continued below 20 Hz asks for boost no door
        # speaker will make and the amplifier should not attempt.
        curve = from_points([(100, 0.0), (1000, -6.0)])
        assert curve.at(np.array([10.0]))[0] == pytest.approx(0.0)
        assert curve.at(np.array([100_000.0]))[0] == pytest.approx(-6.0)

    def test_clamping_can_be_refused(self):
        curve = from_points([(100, 0.0), (1000, -6.0)])
        with pytest.raises(TargetError, match="only defined over"):
            curve.at(np.array([50.0, 500.0]), clamp=False)

    def test_an_in_range_axis_passes_the_strict_check(self):
        curve = from_points([(100, 0.0), (1000, -6.0)])
        curve.at(np.array([100.0, 500.0, 1000.0]), clamp=False)

    @pytest.mark.parametrize("bad", [np.array([]), np.array([-1.0]), np.array([0.0])])
    def test_a_bad_axis_is_rejected(self, bad):
        curve = from_points([(100, 0.0), (1000, -6.0)])
        with pytest.raises(TargetError):
            curve.at(bad)


class TestShapes:
    def test_flat_is_flat(self):
        assert np.all(flat(AXIS).at(AXIS) == 0.0)

    def test_tilt_is_the_requested_slope_per_decade(self):
        curve = tilted(AXIS, tilt_db_per_decade=-3.0, pivot_hz=1000.0)
        got = curve.at(np.array([100.0, 1000.0, 10_000.0]))
        assert got == pytest.approx([3.0, 0.0, -3.0], abs=1e-9)

    def test_the_tilt_pivots_where_asked(self):
        curve = tilted(AXIS, tilt_db_per_decade=-6.0, pivot_hz=250.0)
        assert curve.at(np.array([250.0]))[0] == pytest.approx(0.0)
        assert curve.at(np.array([2500.0]))[0] == pytest.approx(-6.0)

    def test_zero_tilt_is_flat(self):
        curve = tilted(AXIS, tilt_db_per_decade=0.0)
        assert np.allclose(curve.at(AXIS), 0.0)

    def test_the_name_records_the_shape(self):
        assert "-3.0 dB/decade" in tilted(AXIS).name

    @pytest.mark.parametrize("pivot", [0.0, -10.0])
    def test_a_bad_pivot_is_rejected(self, pivot):
        with pytest.raises(TargetError, match="pivot_hz"):
            tilted(AXIS, pivot_hz=pivot)


class TestHarmanIsNotGuessed:
    def test_it_refuses_rather_than_approximating(self):
        # The project's rule is not to state published figures from memory. A
        # target curve is the worst possible thing to approximate: every tune
        # afterwards inherits the error, and no measurement can reveal it,
        # because the tune will faithfully match whatever curve it was given.
        with pytest.raises(NotImplementedError, match="from_points"):
            harman_in_car(AXIS)

    def test_the_message_says_how_to_supply_a_real_one(self):
        with pytest.raises(NotImplementedError) as excinfo:
            harman_in_car(AXIS)
        assert "cite" in str(excinfo.value)


class TestLevelSplit:
    def test_a_pure_offset_is_reported_in_full(self):
        target = tilted(AXIS)
        measured = target.at(AXIS) + 7.5
        assert level_offset_db(measured, target.at(AXIS), AXIS) == pytest.approx(
            7.5, abs=1e-9
        )

    def test_a_matching_measurement_needs_no_offset(self):
        target = tilted(AXIS)
        assert level_offset_db(target.at(AXIS), target.at(AXIS), AXIS) == (
            pytest.approx(0.0, abs=1e-9)
        )

    def test_correction_returns_a_target_at_the_measurement_s_level(self):
        # What the fitter should see: the same shape, no broadband step. If it
        # saw the raw target it would spend bands manufacturing 7.5 dB that a
        # single gain register does exactly.
        target = tilted(AXIS)
        measured = target.at(AXIS) + 7.5
        for_fit, offset = correction_db(measured, target, AXIS)
        assert offset == pytest.approx(7.5, abs=1e-9)
        assert np.allclose(for_fit, measured)

    def test_shape_error_survives_the_level_split(self):
        # A level offset is removed; a real shape error is not. The bump here
        # sits above the level band, so it cannot contaminate the offset and
        # the two effects are cleanly separable.
        target = tilted(AXIS)
        bump = 4.0 * np.exp(-(((np.log(AXIS) - np.log(12_000.0)) / 0.2) ** 2))
        measured = target.at(AXIS) + 7.5 + bump
        for_fit, offset = correction_db(measured, target, AXIS)
        residual = measured - for_fit

        assert offset == pytest.approx(7.5, abs=0.05)
        assert residual.max() > 3.5  # the bump is still there to correct
        assert abs(residual[AXIS < 4000].mean()) < 0.05  # elsewhere, nothing

    def test_an_in_band_bump_does_move_the_offset(self):
        # And it should: the level band's mean is what it says it is. A 4 dB
        # peak inside it genuinely raises the average level, so some of it
        # lands in gain rather than EQ. Worth pinning, because it is the kind
        # of interaction that later looks like a bug.
        target = tilted(AXIS)
        bump = 4.0 * np.exp(-(((np.log(AXIS) - np.log(2000.0)) / 0.2) ** 2))
        measured = target.at(AXIS) + 7.5 + bump
        _, offset = correction_db(measured, target, AXIS)

        assert 7.5 < offset < 8.5
        # The bulk of the bump still reaches the fitter rather than being
        # absorbed into gain.
        for_fit, _ = correction_db(measured, target, AXIS)
        assert (measured - for_fit).max() > 3.0

    def test_the_level_band_excludes_the_extremes(self):
        # Cabin gain at the bottom and seat-to-seat scatter at the top both
        # move the average without saying anything about the shape.
        lo, hi = DEFAULT_LEVEL_BAND_HZ
        assert lo >= 100.0
        assert hi <= 8000.0

        target = flat(AXIS)
        measured = np.zeros_like(AXIS)
        measured[AXIS < 100.0] += 20.0  # a big cabin-gain lift, out of band
        assert level_offset_db(measured, target.at(AXIS), AXIS) == pytest.approx(
            0.0, abs=1e-9
        )

    def test_the_band_mean_is_per_octave_not_per_point(self):
        # On an axis dense at HF, a plain mean would let the top octave set the
        # level for the whole curve.
        axis = np.concatenate(
            [np.geomspace(200.0, 1000.0, 5), np.geomspace(1000.0, 4000.0, 200)]
        )
        values = np.where(axis < 1000.0, 10.0, 0.0)
        got = level_offset_db(values, np.zeros_like(axis), axis, (200.0, 4000.0))
        # Per-octave weighting: 200-1000 Hz is ~2.3 octaves of the ~4.3 total.
        assert 4.0 < got < 6.5

    def test_an_empty_band_is_rejected(self):
        curve = flat(AXIS).at(AXIS)
        with pytest.raises(TargetError, match="no frequency points"):
            level_offset_db(curve, curve, AXIS, (30_000.0, 40_000.0))

    def test_an_inverted_band_is_rejected(self):
        target = flat(AXIS)
        with pytest.raises(TargetError, match="inverted"):
            level_offset_db(target.at(AXIS), target.at(AXIS), AXIS, (4000.0, 200.0))

    def test_mismatched_shapes_are_rejected(self):
        with pytest.raises(TargetError, match="same shape"):
            level_offset_db(np.zeros(10), np.zeros(11), np.geomspace(20, 20e3, 10))
        with pytest.raises(TargetError, match="same shape"):
            correction_db(np.zeros(10), flat(AXIS), AXIS)


class TestNormalisation:
    def test_a_normalised_curve_has_zero_mean_in_band(self):
        curve = tilted(AXIS).shifted(15.0).normalized()
        assert level_offset_db(
            curve.at(AXIS), np.zeros_like(AXIS), AXIS
        ) == pytest.approx(0.0, abs=1e-9)

    def test_normalising_preserves_shape(self):
        curve = tilted(AXIS)
        shifted = curve.normalized()
        delta = curve.at(AXIS) - shifted.at(AXIS)
        assert np.allclose(delta, delta[0])

    def test_shifting_moves_the_whole_curve(self):
        curve = tilted(AXIS)
        assert np.allclose(curve.shifted(3.0).at(AXIS), curve.at(AXIS) + 3.0)

    def test_the_name_survives_transformation(self):
        curve = from_points([(100, 0.0), (1000, -6.0)], name="keepme")
        assert curve.normalized().name == "keepme"
        assert curve.shifted(2.0).name == "keepme"


class TestWithTheFitter:
    """The reason the level split exists, demonstrated end to end."""

    def test_the_split_leaves_the_fitter_only_shape_to_correct(self):
        from tuner.optimize.biquad import FitConstraints, fit, objective

        axis = np.geomspace(100.0, 10_000.0, 200)
        target = tilted(axis, tilt_db_per_decade=-3.0)
        bump = 6.0 * np.exp(-(((np.log(axis) - np.log(1500.0)) / 0.25) ** 2))
        measured = target.at(axis) + 18.0 + bump  # 18 dB hot, plus one bump

        for_fit, offset = correction_db(measured, target, axis)
        assert offset > 15.0

        bands = fit(
            measured,
            for_fit,
            axis,
            sample_rate_hz=48_000,
            constraints=FitConstraints(max_bands=3, max_iterations=60, seed=0),
        )
        after = objective(bands, measured, for_fit, axis, 48_000)
        before = objective((), measured, for_fit, axis, 48_000)
        assert after < before / 2, "the fitter should flatten the bump"

    def test_the_fitter_ignores_level_whether_or_not_it_is_split_out(self):
        """The fitter is level-blind by construction, since 2026-08-12.

        This test used to assert the opposite -- that handing the fitter an
        unsplit target made it "waste itself on level" and score above 5.0.
        That was true and it was the bug: the fitter's cost demanded an
        absolute level match, which a chain of peaking sections cannot make
        without a broadband boost, and boost is the one thing the constraints
        forbid. On M4's known answer it cost 0.81 dB rms against an answer the
        chain could express exactly.

        The shape term is now mean-centred, so a constant in the target is
        invisible to the fit. Both inputs must give the same *shape*.
        """
        from tuner.optimize.biquad import FitConstraints, fit, response_db

        axis = np.geomspace(100.0, 10_000.0, 200)
        target = tilted(axis, tilt_db_per_decade=-3.0)
        bump = 6.0 * np.exp(-(((np.log(axis) - np.log(1500.0)) / 0.25) ** 2))
        measured = target.at(axis) + 18.0 + bump

        con = FitConstraints(max_bands=3, max_iterations=60, seed=0)
        for_fit, offset = correction_db(measured, target, axis)
        assert offset > 15.0

        split = response_db(fit(measured, for_fit, axis, 48_000, con), axis, 48_000)
        unsplit = response_db(
            fit(measured, target.at(axis), axis, 48_000, con), axis, 48_000
        )
        assert np.max(np.abs(split - unsplit)) < 0.01

        # And the reason the split still exists: the 18 dB is real and did not
        # go away. Nothing in the EQ chain addresses it, so the caller must
        # put it in channel gain -- which is what `offset` is for. Measured
        # the same way `offset` was, against the same band.
        _, remaining = correction_db(measured + split, target, axis)
        assert remaining > 15.0
