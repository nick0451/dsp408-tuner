"""Ambient noise, and the three ways it can be mistaken for something else.

Everything before the microphone was a cable, where the noise floor is
stationary and one probe before the sweep was a fair sample of the whole
capture. A room is neither, and the failures it causes do not look like
failures -- they look like compression, or like a system that is better than
it is.
"""

from __future__ import annotations

import numpy as np
import pytest

from tuner.measure.qa import (
    DEFAULT_MIN_TONE_HEADROOM_DB,
    IndeterminateLinearity,
    LinearityResult,
    NonLinearPath,
    analyze_idle_noise,
    require_linear_path,
)

SR = 48_000
FREQS = (300.0, 1000.0, 3000.0)
LEVELS = (-40.0, -30.0, -20.0)


def an_idle_capture(
    rms_dbfs: float = -80.0,
    tone_hz: float | None = None,
    tone_dbfs: float = -70.0,
    seed: int = 0,
) -> np.ndarray:
    """A second of silence, optionally with a narrowband interferer in it."""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, 1.0, SR)
    noise = noise / float(np.sqrt(np.mean(noise**2))) * 10 ** (rms_dbfs / 20.0)
    if tone_hz is not None:
        t = np.arange(SR) / SR
        noise = noise + 10 ** (tone_dbfs / 20.0) * np.sin(2 * np.pi * tone_hz * t)
    return noise


def gains_for(captured_dbfs: np.ndarray) -> LinearityResult:
    """Build a result from the level each tone *arrived* at."""
    return LinearityResult(
        freqs_hz=FREQS,
        levels_dbfs=LEVELS,
        gain_db=captured_dbfs - np.asarray(LEVELS, dtype=np.float64),
    )


def a_linear_path(gain_db: float = -30.0) -> np.ndarray:
    """Constant gain: captured level tracks stimulus level exactly.

    Shaped (frequencies, levels), which is what LinearityResult expects.
    """
    levels = np.asarray(LEVELS, dtype=np.float64)
    return np.tile(levels + gain_db, (len(FREQS), 1))


def contaminate(captured_dbfs: np.ndarray, row: int, interferer_dbfs: float):
    """Add an interferer's power to one tone, at every stimulus level.

    Power, not amplitude: an interferer uncorrelated with the tone adds in
    power, which is what makes its effect largest where the tone is weakest.
    """
    out = captured_dbfs.copy()
    signal = 10 ** (out[row] / 10.0)
    out[row] = 10.0 * np.log10(signal + 10 ** (interferer_dbfs / 10.0))
    return out


class TestBroadbandAmbientIsNotTheRisk:
    """Measured, after claiming twice that it was.

    The linearity detector reads a single FFT bin over a 1.1 s window. That
    is a 1.36 Hz effective noise bandwidth against 24 kHz -- **42.5 dB of
    rejection** against anything broadband. A tone captured at -70 dBFS is
    only troubled once the broadband floor reaches about -27 dBFS *at the
    input*, which is not a room, it is a fault.

    Keeping this as a test rather than a comment because the intuition that
    room noise threatens a narrowband measurement is strong, wrong, and was
    acted on here before it was checked.
    """

    def test_a_loud_room_does_not_move_a_narrowband_measurement(self):
        idle = analyze_idle_noise(an_idle_capture(rms_dbfs=-60.0), SR)
        per_bin = idle.level_at(np.array(FREQS))
        assert np.all(per_bin < -95.0), "broadband noise concentrated in a bin?"
        assert float(np.max(per_bin)) < idle.rms_dbfs - 35.0

    def test_and_such_a_path_still_reads_linear(self):
        clean = gains_for(a_linear_path())
        idle = analyze_idle_noise(an_idle_capture(rms_dbfs=-60.0), SR)
        require_linear_path(clean, idle=idle)


class TestNarrowbandInterferenceIsTheRisk:
    """One bin has no rejection at all, and the relative guard cannot see it.

    Mains harmonics, a fan blade tone, motor whine -- all put their energy in
    a few bins. Landing on a test frequency, an interferer inflates that
    tone's apparent level, and because it does not scale with stimulus it
    inflates it *most at the lowest level*. Gain then falls as level rises,
    which is the signature of compression.

    ``usable`` cannot exclude it. That test only drops tones that are too
    **quiet** relative to their neighbours, and an interferer makes a tone too
    **loud**.
    """

    #: A 300 Hz interferer -- the fifth harmonic of 60 Hz mains -- sitting on
    #: the lowest test tone, at the level that tone arrives at.
    INTERFERER_HZ = 300.0
    INTERFERER_DBFS = -70.0

    def _contaminated(self) -> LinearityResult:
        return gains_for(contaminate(a_linear_path(), 0, self.INTERFERER_DBFS))

    def test_it_reads_as_compression_on_a_linear_path(self):
        with pytest.raises(NonLinearPath, match="varies by"):
            require_linear_path(self._contaminated())

    def test_the_relative_guard_keeps_the_contaminated_tone(self):
        # The gap, stated as an assertion: nothing is excluded, so the false
        # alarm above goes through.
        assert self._contaminated().usable().all()

    def test_the_absolute_floor_excludes_it_and_the_path_reads_linear(self):
        """The fix, end to end.

        Judged against the floor measured at its own frequency, the 300 Hz
        tone has 3 dB of margin rather than 40, so it is dropped -- and the
        two clean tones that remain agree exactly, which is the truth.
        """
        idle = analyze_idle_noise(
            an_idle_capture(tone_hz=self.INTERFERER_HZ, tone_dbfs=self.INTERFERER_DBFS),
            SR,
        )
        assert list(self._contaminated().usable_against(idle)) == [False, True, True]
        require_linear_path(self._contaminated(), idle=idle)

    def test_the_floor_shows_the_interferer_where_it_is(self):
        idle = analyze_idle_noise(
            an_idle_capture(tone_hz=self.INTERFERER_HZ, tone_dbfs=self.INTERFERER_DBFS),
            SR,
        )
        at = idle.level_at(np.array(FREQS))
        assert at[0] == pytest.approx(self.INTERFERER_DBFS, abs=3.0)
        assert np.all(at[1:] < -95.0)

    def test_two_contaminated_tones_leave_too_little_to_conclude(self):
        """Excluding is not the same as concluding.

        With only one clean tone left, level-independence means nothing --
        one tone can fail to contradict linearity but cannot establish it.
        """
        both = gains_for(
            contaminate(
                contaminate(a_linear_path(), 0, self.INTERFERER_DBFS),
                1,
                self.INTERFERER_DBFS,
            )
        )
        idle = analyze_idle_noise(
            an_idle_capture(tone_hz=self.INTERFERER_HZ, tone_dbfs=self.INTERFERER_DBFS)
            + 10 ** (self.INTERFERER_DBFS / 20.0)
            * np.sin(2 * np.pi * 1000.0 * np.arange(SR) / SR),
            SR,
        )
        with pytest.raises(IndeterminateLinearity, match="carried signal above"):
            require_linear_path(both, idle=idle)

    def test_indeterminate_is_not_a_pass(self):
        assert not issubclass(IndeterminateLinearity, NonLinearPath)


class TestTheIdleFloorAtAFrequency:
    def test_it_reports_roughly_the_right_level(self):
        idle = analyze_idle_noise(an_idle_capture(-70.0), SR)
        at = idle.level_at(np.array([300.0, 1000.0, 3000.0]))
        # White noise spread over the band: per-bin level is far below the
        # broadband rms, and what matters is that it is finite and stable.
        assert np.all(np.isfinite(at))
        assert float(np.ptp(at)) < 25.0

    def test_a_louder_room_reads_louder_at_every_frequency(self):
        quiet = analyze_idle_noise(an_idle_capture(-90.0, seed=1), SR)
        loud = analyze_idle_noise(an_idle_capture(-60.0, seed=1), SR)
        probe = np.array([300.0, 1000.0, 3000.0])
        assert np.all(loud.level_at(probe) > quiet.level_at(probe) + 25.0)

    def test_a_result_with_no_spectrum_says_so(self):
        from tuner.measure.qa import IdleNoiseResult

        bare = IdleNoiseResult(-80.0, -70.0, SR, {})
        with pytest.raises(ValueError, match="no spectrum"):
            bare.level_at(np.array([1000.0]))

    def test_the_two_margins_mean_different_things(self):
        """40 dB is a judgement call; 20 dB is arithmetic.

        The relative figure asks "is this tone in the mud compared to its
        siblings", which has no exact answer. The absolute one asks how much
        the floor moves the measurement, which does: a floor x dB down
        inflates a level by 10*log10(1 + 10**(-x/10)).

        Pinned so that changing either is a deliberate edit, and so the
        derivation cannot drift away from the number it produced.
        """
        from tuner.measure.qa import DEFAULT_MIN_TONE_MARGIN_DB

        assert DEFAULT_MIN_TONE_HEADROOM_DB == 40.0
        assert DEFAULT_MIN_TONE_MARGIN_DB == 20.0

        induced = 10 * np.log10(1 + 10 ** (-DEFAULT_MIN_TONE_MARGIN_DB / 10))
        assert induced == pytest.approx(0.043, abs=0.002)
        # Comfortably inside the tolerance the check is run with.
        assert induced < 0.1

    def test_reusing_the_relative_figure_would_exclude_a_measurable_room(self):
        # Which is what happened: 40 dB against a per-bin floor dropped every
        # tone in a room quiet enough to measure in.
        from tuner.measure.qa import DEFAULT_MIN_TONE_HEADROOM_DB as STRICT

        clean = gains_for(a_linear_path())
        idle = analyze_idle_noise(an_idle_capture(rms_dbfs=-60.0), SR)
        assert not clean.usable_against(idle, min_margin_db=STRICT).any()
        assert clean.usable_against(idle).all()


class TestTheSpreadBetweenRepeats:
    """The only instrument that sees noise *during* the sweep."""

    def _passes(self, n: int, contaminate: int = 0, seed: int = 0):
        from tuner.measure import capture as capture_mod

        rng = np.random.default_rng(seed)
        base = np.zeros(4096)
        base[100] = 1.0
        stack = []
        for i in range(n):
            passing = base.copy()
            if i < contaminate:
                passing = passing + rng.normal(0.0, 0.05, base.size)
            stack.append(passing)
        return capture_mod._pass_spread(stack, SR)

    def test_identical_passes_disagree_by_nothing(self):
        spread = self._passes(3)
        assert spread is not None
        assert spread.n_passes == 3
        assert spread.worst_db(20.0, 20_000.0) < 1e-6

    def test_one_contaminated_pass_shows_up(self):
        spread = self._passes(3, contaminate=1)
        assert spread.worst_db(20.0, 20_000.0) > 0.5

    def test_a_single_pass_has_no_spread_to_report(self):
        # Not zero -- unknown. One measurement cannot disagree with itself,
        # and reporting 0.0 would claim a cleanliness nobody measured.
        assert self._passes(1) is None

    def test_it_interpolates_to_an_arbitrary_axis(self):
        spread = self._passes(3, contaminate=1)
        axis = np.geomspace(50.0, 15_000.0, 40)
        assert spread.at(axis).shape == axis.shape
        assert np.all(np.isfinite(spread.at(axis)))

    def test_the_summary_says_median_and_worst(self):
        text = self._passes(3, contaminate=1).summary()
        assert "median" in text and "worst" in text and "3 passes" in text

    def test_it_reaches_the_measurement(self, monkeypatch):
        from tuner.measure import capture as capture_mod

        monkeypatch.setattr(
            capture_mod,
            "play_record",
            lambda stimulus, *a, **kw: np.tile(
                np.concatenate([stimulus, np.zeros(1000)])[:, None], (1, 1)
            ),
        )
        config = capture_mod.CaptureConfig(
            sample_rate_hz=SR,
            duration_s=0.2,
            tail_s=0.0,
            repeats=3,
            ramp=False,
            verify_quiet=False,
            input_channels=(0,),
        )
        session = capture_mod.SessionInfo(
            gains_db=(0.0,), temperature_c=21.0, setup_token="bench, unmoved"
        )
        result = capture_mod.capture_sweep(config, session)[0]
        assert result.repeat_spread is not None
        assert result.repeat_spread.n_passes == 3

    def test_a_single_repeat_leaves_it_unset(self, monkeypatch):
        from tuner.measure import capture as capture_mod

        monkeypatch.setattr(
            capture_mod,
            "play_record",
            lambda stimulus, *a, **kw: np.concatenate([stimulus, np.zeros(1000)])[
                :, None
            ],
        )
        config = capture_mod.CaptureConfig(
            sample_rate_hz=SR,
            duration_s=0.2,
            tail_s=0.0,
            repeats=1,
            ramp=False,
            verify_quiet=False,
            input_channels=(0,),
        )
        session = capture_mod.SessionInfo(
            gains_db=(0.0,), temperature_c=21.0, setup_token="bench, unmoved"
        )
        result = capture_mod.capture_sweep(config, session)[0]
        assert result.repeat_spread is None
