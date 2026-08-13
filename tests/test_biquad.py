"""Tests for PEQ fitting.

Fit quality is checked by **known answer**: plant a filter, ask the fitter to
cancel it, and require the residual to be small. That catches sign errors,
coefficient mistakes and warping bugs in a way that asserting "the cost went
down" never would -- a wrong fitter also reduces its own cost.

Fits here use deliberately small band counts, short frequency axes and low
iteration caps. The suite must stay fast; convergence at production settings is
measured in docs/STATE.md, not asserted here.
"""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest
from scipy import signal

from tuner.dsp.backend import Biquad, DeviceLimits, FilterType
from tuner.dsp.protocol import bandwidth_octaves, q_from_bw_raw
from tuner.measure import log_freqs
from tuner.optimize.biquad import (
    NEGLIGIBLE_GAIN_DB,
    FitConstraints,
    bandwidth_octaves_from_q,
    biquad_coefficients,
    constraints_for,
    fit,
    objective,
    q_from_bandwidth_octaves,
    response_db,
)

FS = 48_000

#: The DSP-408's measured grids. See docs/dsp408-protocol.md.
DEVICE = FitConstraints(
    max_bands=2,
    bandwidth_step_octaves=0.01,
    bandwidth_min_octaves=0.05,
    freq_step_hz=1.0,
    gain_step_db=0.1,
    max_iterations=40,
)


def _axis(points: int = 80) -> np.ndarray:
    return log_freqs(20.0, 20_000.0, points)


class TestBandwidthAndQ:
    @pytest.mark.parametrize("octaves", [0.05, 0.29, 0.57, 1.39, 3.0])
    def test_round_trip(self, octaves):
        q = q_from_bandwidth_octaves(octaves)
        assert bandwidth_octaves_from_q(q) == pytest.approx(octaves)

    def test_narrower_bandwidth_is_higher_q(self):
        assert q_from_bandwidth_octaves(0.1) > q_from_bandwidth_octaves(1.0)

    def test_agrees_with_the_device_encoding(self):
        # protocol.py holds the DSP-408's raw-integer form of the same maths.
        # If these ever diverge, one of them is wrong and the optimizer would
        # be solving on a grid the hardware does not have.
        for bw_raw in (0, 24, 43, 52, 90, 134, 250):
            octaves = bandwidth_octaves(bw_raw)
            assert q_from_bandwidth_octaves(octaves) == pytest.approx(
                q_from_bw_raw(bw_raw), rel=1e-12
            )

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_rejects_nonsense(self, bad):
        with pytest.raises(ValueError):
            q_from_bandwidth_octaves(bad)
        with pytest.raises(ValueError):
            bandwidth_octaves_from_q(bad)


class TestCoefficients:
    @pytest.mark.parametrize("kind", list(FilterType))
    def test_match_scipy_freqz(self, kind):
        band = Biquad(freq_hz=1000.0, gain_dbfs=6.0, q=2.0, kind=kind)
        b, a = biquad_coefficients(band, FS)
        freqs = _axis()
        _, h = signal.freqz(b, a, worN=freqs, fs=FS)
        ours = response_db((band,), freqs, FS)
        assert np.allclose(ours, 20.0 * np.log10(np.abs(h)), atol=1e-9)

    def test_normalised_so_a0_is_one(self):
        _, a = biquad_coefficients(Biquad(1000.0, 3.0, 1.0), FS)
        assert a[0] == pytest.approx(1.0)

    def test_peaking_gain_is_exact_at_centre(self):
        band = Biquad(freq_hz=1000.0, gain_dbfs=6.0, q=2.0)
        assert response_db((band,), np.array([1000.0]), FS)[0] == pytest.approx(
            6.0, abs=1e-9
        )

    def test_a_cut_is_the_exact_inverse_of_a_boost(self):
        # RBJ peaking with gain 1/A has numerator and denominator swapped, so
        # the pair cancels exactly. This is what makes the known-answer fit
        # test below meaningful.
        freqs = _axis()
        up = Biquad(freq_hz=800.0, gain_dbfs=5.0, q=1.7)
        down = replace(up, gain_dbfs=-5.0)
        assert np.allclose(response_db((up, down), freqs, FS), 0.0, atol=1e-9)

    def test_rejects_centre_at_or_above_nyquist(self):
        with pytest.raises(ValueError, match="Nyquist"):
            biquad_coefficients(Biquad(freq_hz=FS / 2, gain_dbfs=0.0, q=1.0), FS)

    def test_rejects_non_positive_q(self):
        with pytest.raises(ValueError, match="Q must be positive"):
            biquad_coefficients(Biquad(freq_hz=1000.0, gain_dbfs=0.0, q=0.0), FS)


class TestResponse:
    def test_empty_chain_is_flat(self):
        assert np.allclose(response_db((), _axis(), FS), 0.0)

    def test_disabled_bands_contribute_nothing(self):
        freqs = _axis()
        band = Biquad(freq_hz=1000.0, gain_dbfs=6.0, q=2.0, enabled=False)
        assert np.allclose(response_db((band,), freqs, FS), 0.0)

    def test_chain_sums_in_db(self):
        freqs = _axis()
        a = Biquad(freq_hz=200.0, gain_dbfs=3.0, q=1.0)
        b = Biquad(freq_hz=5000.0, gain_dbfs=-4.0, q=2.0)
        assert np.allclose(
            response_db((a, b), freqs, FS),
            response_db((a,), freqs, FS) + response_db((b,), freqs, FS),
        )

    def test_rejects_an_empty_axis(self):
        with pytest.raises(ValueError, match="non-empty"):
            response_db((), np.array([]), FS)


class TestFitConstraints:
    def test_band_count_comes_from_the_device(self):
        limits = DeviceLimits(max_peq_per_channel=4)
        assert constraints_for(limits).max_bands == 4

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_bands": 0},
            {"min_q": 0.0},
            {"min_q": 9.0, "max_q": 2.0},
            {"freq_range_hz": (2000.0, 200.0)},
            {"max_boost_db": -1.0},
            {"max_cut_db": -1.0},
        ],
    )
    def test_rejects_incoherent_settings(self, kwargs):
        with pytest.raises(ValueError):
            FitConstraints(**kwargs)


class TestFit:
    def test_cancels_a_planted_peak(self):
        freqs = _axis()
        planted = (Biquad(freq_hz=1000.0, gain_dbfs=6.0, q=2.0),)
        measured = response_db(planted, freqs, FS)
        target = np.zeros_like(measured)

        bands = fit(measured, target, freqs, FS, DEVICE)
        residual = measured + response_db(bands, freqs, FS) - target

        before = np.sqrt(np.mean(measured**2))
        after = np.sqrt(np.mean(residual**2))
        assert after < before / 10, f"{before:.3f} dB rms -> {after:.3f} dB rms"

    def test_is_deterministic(self):
        # An unrepeatable optimizer result cannot be compared across sessions,
        # which the provenance rules effectively forbid.
        freqs = _axis()
        measured = response_db((Biquad(1500.0, 5.0, 2.0),), freqs, FS)
        target = np.zeros_like(measured)
        first = fit(measured, target, freqs, FS, DEVICE)
        second = fit(measured, target, freqs, FS, DEVICE)
        assert first == second

    def test_never_exceeds_the_band_budget(self):
        freqs = _axis()
        measured = response_db(
            (
                Biquad(100.0, 6.0, 2.0),
                Biquad(1000.0, -5.0, 3.0),
                Biquad(9000.0, 4.0, 1.5),
            ),
            freqs,
            FS,
        )
        bands = fit(measured, np.zeros_like(measured), freqs, FS, DEVICE)
        assert len(bands) <= DEVICE.max_bands

    def test_prunes_bands_that_do_nothing(self):
        # A flat measurement needs no correction, so no hardware should be
        # spent on it.
        freqs = _axis()
        flat = np.zeros(freqs.size)
        bands = fit(flat, flat, freqs, FS, DEVICE)
        assert all(abs(b.gain_dbfs) >= NEGLIGIBLE_GAIN_DB for b in bands)

    def test_respects_the_boost_ceiling(self):
        # A deep null must not be filled beyond the cap, however much it would
        # improve the raw error.
        freqs = _axis()
        measured = response_db((Biquad(1000.0, -12.0, 2.0),), freqs, FS)
        constraints = replace(DEVICE, max_boost_db=3.0)
        bands = fit(measured, np.zeros_like(measured), freqs, FS, constraints)
        assert all(b.gain_dbfs <= constraints.max_boost_db + 1e-9 for b in bands)

    def test_output_sits_on_the_device_grids(self):
        # The whole reason the search runs in the bandwidth domain: a returned
        # value the hardware cannot store would be silently altered on write.
        freqs = _axis()
        measured = response_db((Biquad(700.0, 7.0, 2.5),), freqs, FS)
        bands = fit(measured, np.zeros_like(measured), freqs, FS, DEVICE)
        assert bands, "expected at least one band for a 7 dB peak"
        for band in bands:
            assert band.freq_hz == pytest.approx(round(band.freq_hz))
            octaves = bandwidth_octaves_from_q(band.q)
            steps = (
                octaves - DEVICE.bandwidth_min_octaves
            ) / DEVICE.bandwidth_step_octaves
            assert steps == pytest.approx(round(steps), abs=1e-6)
            assert band.gain_dbfs * 10 == pytest.approx(round(band.gain_dbfs * 10))

    def test_stays_inside_the_q_bounds(self):
        freqs = _axis()
        measured = response_db((Biquad(700.0, 7.0, 6.0),), freqs, FS)
        constraints = replace(DEVICE, min_q=0.7, max_q=4.0)
        bands = fit(measured, np.zeros_like(measured), freqs, FS, constraints)
        for band in bands:
            assert constraints.min_q - 1e-6 <= band.q <= constraints.max_q + 1e-6

    def test_rejects_mismatched_shapes(self):
        freqs = _axis(50)
        with pytest.raises(ValueError, match="same shape"):
            fit(np.zeros(50), np.zeros(49), freqs, FS, DEVICE)

    def test_rejects_frequencies_at_or_above_nyquist(self):
        freqs = np.array([100.0, FS / 2])
        with pytest.raises(ValueError, match="Nyquist"):
            fit(np.zeros(2), np.zeros(2), freqs, FS, DEVICE)

    def test_a_target_curve_is_followed_not_just_flatness(self):
        # Fitting toward a sloped target is the real use: house curves are not
        # flat. A fitter that only ever flattens would pass every other test.
        freqs = _axis()
        measured = np.zeros(freqs.size)
        target = -3.0 * np.log2(freqs / 20.0) / np.log2(1000.0 / 20.0)
        bands = fit(measured, target, freqs, FS, DEVICE)
        residual = measured + response_db(bands, freqs, FS) - target
        before = np.sqrt(np.mean((measured - target) ** 2))
        assert np.sqrt(np.mean(residual**2)) < before


class TestGreedySeedIsNotTheAnswerOnItsOwn:
    def test_search_improves_on_the_seed(self):
        # If differential evolution were adding nothing, the greedy pass alone
        # would do and the cost of the search would not be justified.
        from tuner.optimize.biquad import _bands_from_vector, _greedy_seed

        freqs = _axis()
        planted = (Biquad(300.0, 7.0, 2.0), Biquad(4000.0, -6.0, 3.0))
        measured = response_db(planted, freqs, FS)
        target = np.zeros_like(measured)

        seed_vector = _greedy_seed(measured - target, freqs, FS, DEVICE)
        seeded = _bands_from_vector(seed_vector, DEVICE)
        fitted = fit(measured, target, freqs, FS, DEVICE)

        def cost(bands):
            return objective(bands, measured, target, freqs, FS, DEVICE)

        assert cost(fitted) < cost(seeded)


def test_octave_maths_matches_a_hand_worked_value():
    # One octave of bandwidth is Q = sqrt(2)/1, a value that can be checked
    # without trusting the implementation that produced it.
    assert q_from_bandwidth_octaves(1.0) == pytest.approx(math.sqrt(2.0))


class TestObjective:
    """The objective is public because RMS alone is the wrong yardstick."""

    def test_penalises_boost_more_than_cut(self):
        freqs = _axis()
        flat = np.zeros(freqs.size)
        boost = (Biquad(1000.0, 4.0, 2.0),)
        cut = (Biquad(1000.0, -4.0, 2.0),)
        # Symmetric errors, asymmetric cost: that asymmetry is the whole point.
        assert objective(boost, flat, flat, freqs, FS) > objective(
            cut, flat, flat, freqs, FS
        )

    def test_a_lower_rms_can_be_a_worse_tune(self):
        # Filling a deep null gets closer to target and is still rejected.
        freqs = _axis()
        measured = response_db((Biquad(1000.0, -9.0, 2.0),), freqs, FS)
        target = np.zeros_like(measured)
        filled = (Biquad(1000.0, 9.0, 2.0),)
        left_alone = ()

        def rms(b):
            return np.sqrt(
                np.mean((measured + response_db(b, freqs, FS) - target) ** 2)
            )

        assert rms(filled) < rms(left_alone)
        assert objective(filled, measured, target, freqs, FS) > objective(
            left_alone, measured, target, freqs, FS
        )

    def test_matches_the_internal_cost_used_during_fitting(self):
        # fit() reuses a precomputed unit circle for speed. If that fast path
        # ever drifts from the public objective, the optimizer would be
        # minimising something other than what callers can inspect.
        from tuner.optimize.biquad import _bands_from_vector, _greedy_seed

        freqs = _axis()
        measured = response_db((Biquad(500.0, 6.0, 2.0),), freqs, FS)
        target = np.zeros_like(measured)
        bands = _bands_from_vector(
            _greedy_seed(measured - target, freqs, FS, DEVICE), DEVICE
        )
        direct = objective(bands, measured, target, freqs, FS, DEVICE)

        correction = response_db(bands, freqs, FS)
        residual = measured - target + correction
        # Mean-centred shape term, uncentred boost term -- see objective().
        residual = residual - np.mean(residual)
        expected = float(
            np.mean(residual**2)
            + DEVICE.boost_penalty_weight * np.mean(np.clip(correction, 0.0, None) ** 2)
        )
        assert direct == pytest.approx(expected)


class TestM4sKnownAnswer:
    """The fit M4 got wrong on hardware, reduced to a unit test.

    The closed loop's target was OUT1's own response with one band installed
    -- 2514 Hz, -12.00 dB, bw_raw 42 (0.47 octaves), the operator's own EQ.
    The chain can express that **exactly**, so the right score was ~0. The run
    scored 1.034 and wrote two straddling notches instead.

    The first explanation written down was that -12.00 dB sits on
    ``max_cut_db`` and differential evolution converges badly onto a bound.
    Reproducing it offline **refuted that**: on a 30-3500 Hz axis the fitter
    recovered the band to 0.004 dB rms with the bound untouched.

    The real cause was the fit's cost demanding an absolute level match. The
    target's own mean sits 2.46 dB below the measurement's -- a -12 dB notch
    over a three-octave band moves the mean that far -- so the correction the
    fitter was asked for was "the notch, plus 2.46 dB everywhere". A peaking
    chain cannot make a broadband boost, ``max_boost_db`` is 3.0, and boost
    carries a 4x penalty. So it ate the offset as residual.

    And ``MagnitudeObjective`` re-level-matches before scoring, so that
    constant was **invisible to the verdict and mandatory in the fit**.
    """

    LO, HI = 450.0, 3500.0
    FREQ_HZ, GAIN_DB, OCTAVES = 2514.0, -12.0, 0.47

    def _case(self):
        axis = np.exp(np.linspace(math.log(self.LO), math.log(self.HI), 300))
        answer = Biquad(
            freq_hz=self.FREQ_HZ,
            gain_dbfs=self.GAIN_DB,
            q=q_from_bandwidth_octaves(self.OCTAVES),
            kind=FilterType.PEAKING,
        )
        ideal = response_db((answer,), axis, FS)
        raw = np.zeros_like(axis)
        # Level-matched exactly as tuner.optimize.target.correction_db does,
        # which is what run.py hands the fitter.
        from tuner.optimize.target import correction_db, from_points

        target, offset = correction_db(
            raw,
            from_points(list(zip(axis.tolist(), ideal.tolist(), strict=True)), "ref"),
            axis,
            (self.LO, self.HI),
        )
        return axis, raw, ideal, target, offset

    def _constraints(self, **kw):
        return FitConstraints(
            max_bands=10,
            bandwidth_step_octaves=0.01,
            bandwidth_min_octaves=0.05,
            freq_step_hz=1.0,
            gain_step_db=0.1,
            **kw,
        )

    def test_the_level_offset_is_what_made_this_hard(self):
        # Pins the mechanism, not just the symptom: a narrow deep feature in
        # the target moves its band mean by more than max_boost_db, so the
        # uncentred cost asked for something the constraints forbid.
        _, _, _, _, offset = self._case()
        assert offset == pytest.approx(2.46, abs=0.05)
        assert offset < 3.0  # ...and under max_boost_db, so it looks harmless

    def test_one_band_in_and_one_band_out(self):
        """The answer is one filter, so the fit should be one filter.

        Both halves of this are load-bearing and neither alone is enough.

        Asserting only that *a* band lands near 2514 Hz is **vacuous** --
        checked by reverting the fix, and the broken fitter placed one there
        too. What it also did was surround it with nine more, chasing the
        2.46 dB of level it could not make, and eight of those nine sat
        outside the 450-3500 Hz axis entirely, where no measurement
        constrains them and every one is a real filter once written.

        Asserting only the count would pass a fitter that returned one wrong
        band. Together they pin the answer.
        """
        axis, raw, ideal, target, _ = self._case()
        bands = fit(raw, target, axis, FS, self._constraints())

        assert len(bands) <= 2, f"one band was the whole answer; got {bands}"
        assert not [b for b in bands if not self.LO <= b.freq_hz <= self.HI], (
            "a band was fitted outside the measured axis, from no data"
        )

        deep = [b for b in bands if b.gain_dbfs < -6.0]
        assert len(deep) == 1, f"expected one deep band near 2514 Hz, got {bands}"
        got = deep[0]
        assert got.freq_hz == pytest.approx(self.FREQ_HZ, rel=0.02)
        assert got.gain_dbfs == pytest.approx(self.GAIN_DB, abs=0.5)
        assert bandwidth_octaves_from_q(got.q) == pytest.approx(self.OCTAVES, abs=0.05)

    def test_the_response_matches_the_answer_it_could_express_exactly(self):
        # Assert on **peak**, not on the objective. A 6 dB narrowband error
        # moves this objective by a quarter of a decibel; the project has
        # already shipped one vacuous test that way.
        axis, raw, ideal, target, _ = self._case()
        bands = fit(raw, target, axis, FS, self._constraints())
        achieved = raw + response_db(tuple(bands), axis, FS)
        residual = achieved - ideal
        residual = residual - np.mean(residual)  # level is gain's job
        assert float(np.max(np.abs(residual))) < 0.3  # broken: 1.28 dB
        assert float(np.sqrt(np.mean(residual**2))) < 0.1  # broken: 0.81 dB

    def test_a_constant_in_the_target_changes_nothing(self):
        # The invariant behind the fix. Level is channel gain's job, so two
        # targets differing by a constant must produce the same filters.
        axis, raw, _, target, _ = self._case()
        con = self._constraints()
        a = response_db(fit(raw, target, axis, FS, con), axis, FS)
        b = response_db(fit(raw, target + 7.0, axis, FS, con), axis, FS)
        assert float(np.max(np.abs(a - b))) < 0.01


class TestPruning:
    """Backward elimination, and why it is asymmetric.

    The search has no way to decline a band -- a ``Biquad`` has no "off" the
    optimizer can select -- so it must place all ``max_bands`` filters and the
    surplus goes wherever costs least. Pruning is what turns that back into an
    honest band count.
    """

    def _cost(self, deltas: dict[float, float]):
        """A stub chain cost: each band's absence adds ``deltas[freq]``.

        A stub rather than a real response, so the thresholds under test are
        the only thing that can decide the outcome.
        """

        def cost(chain: tuple[Biquad, ...]) -> float:
            present = {b.freq_hz for b in chain}
            return sum(d for f, d in deltas.items() if f not in present)

        return cost

    def test_a_band_that_earns_nothing_is_dropped(self):
        from tuner.optimize.biquad import PRUNE_TOLERANCE_DB, _prune

        bands = (Biquad(1000.0, -6.0, 2.0), Biquad(2000.0, -0.5, 2.0))
        # Losing 1000 Hz costs a lot; losing 2000 Hz costs almost nothing.
        cost = self._cost({1000.0: 4.0, 2000.0: (PRUNE_TOLERANCE_DB / 2) ** 2})
        assert _prune(bands, cost) == (bands[0],)

    def test_a_band_that_earns_its_slot_is_kept(self):
        from tuner.optimize.biquad import PRUNE_TOLERANCE_DB, _prune

        bands = (Biquad(1000.0, -6.0, 2.0), Biquad(2000.0, -3.0, 2.0))
        cost = self._cost({1000.0: 4.0, 2000.0: (PRUNE_TOLERANCE_DB * 4) ** 2})
        assert _prune(bands, cost) == bands

    def test_an_unmeasured_band_must_earn_far_more(self):
        """The asymmetry. Same contribution, opposite verdicts.

        A band centred outside the measured axis has no evidence for it at its
        own centre frequency -- only for the part of its skirt that reaches
        into the axis. Keeping it on the same terms as a measured band applies
        an evidence standard the data cannot support, and the hazard is
        concrete: fitting a 450-3500 Hz passband has produced +2.1 dB at
        7694 Hz, which on a tweeter is real output the stimulus ceiling never
        accounted for.
        """
        from tuner.optimize.biquad import (
            PRUNE_TOLERANCE_DB,
            UNMEASURED_PRUNE_TOLERANCE_DB,
            _prune,
        )

        # A concrete contribution, not one derived from the constants -- a
        # midpoint would rescale with them and pass for any pair at all,
        # which is the shape of a test that verifies nothing.
        worth_db = 0.2
        assert PRUNE_TOLERANCE_DB < worth_db < UNMEASURED_PRUNE_TOLERANCE_DB

        anchor, candidate = Biquad(1000.0, -6.0, 2.0), Biquad(9000.0, 2.0, 2.0)
        cost = self._cost({1000.0: 4.0, 9000.0: worth_db**2})

        # In band, that contribution is worth a slot.
        assert _prune((anchor, candidate), cost, lambda b: True) == (anchor, candidate)
        # Outside it, the identical contribution is not.
        measured = lambda b: b.freq_hz <= 3500.0  # noqa: E731
        assert _prune((anchor, candidate), cost, measured) == (anchor,)

    def test_pruning_stops_rather_than_emptying_the_chain(self):
        from tuner.optimize.biquad import _prune

        bands = (Biquad(1000.0, -6.0, 2.0),)
        assert _prune(bands, self._cost({1000.0: 9.0})) == bands
