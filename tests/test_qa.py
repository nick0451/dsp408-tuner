"""Tests for rig verification checks.

The linearity check exists because a gated capture path produced four
convincing, entirely wrong frequency responses before anyone thought to sweep
the level. These tests stub the audio layer with linear and non-linear systems
and confirm the check can tell them apart.
"""

import numpy as np
import pytest

import tuner.measure.qa as qa
from tuner.measure.qa import (
    IndeterminateLinearity,
    LinearityResult,
    NonLinearPath,
    measure_level_linearity,
    require_linear_path,
)
from tuner.safety.limits import ChannelLimit

SR = 44_100


class FakePath:
    """Synthetic signal path with optional level-dependent gain."""

    def __init__(self, gain_db=-20.0, gate_threshold_dbfs=None, gate_depth_db=60.0):
        self.gain_db = gain_db
        self.gate_threshold_dbfs = gate_threshold_dbfs
        self.gate_depth_db = gate_depth_db

    def __call__(
        self,
        stimulus,
        output_channel,
        input_channels,
        sample_rate_hz,
        device=None,
        loopback=None,
        tail_s=1.0,
        max_peak_dbfs=0.0,
    ):
        peak = float(np.max(np.abs(stimulus)))
        level_dbfs = 20 * np.log10(peak) if peak > 0 else -200.0
        gain = self.gain_db
        if (
            self.gate_threshold_dbfs is not None
            and level_dbfs < self.gate_threshold_dbfs
        ):
            gain -= self.gate_depth_db
        return (stimulus * 10 ** (gain / 20.0)).reshape(-1, 1)


def run(path, monkeypatch, **kw):
    monkeypatch.setattr(qa, "play_record", path)
    return measure_level_linearity(
        sample_rate_hz=SR,
        output_channel=0,
        input_channel=1,
        limit=ChannelLimit(ceiling_dbfs=-6.0, characterized=True),
        duration_s=0.4,
        settle_s=0.1,
        **kw,
    )


class TestLinearPath:
    def test_measures_constant_gain(self, monkeypatch):
        r = run(FakePath(gain_db=-20.0), monkeypatch)
        assert r.spread_db < 0.2
        assert r.is_linear
        assert np.allclose(r.gain_db, -20.0, atol=0.2)

    def test_passes_the_requirement(self, monkeypatch):
        require_linear_path(run(FakePath(), monkeypatch))


class TestGatedPath:
    def test_detects_a_gate(self, monkeypatch):
        r = run(FakePath(gate_threshold_dbfs=-15.0), monkeypatch)
        assert r.spread_db > 50
        assert not r.is_linear

    def test_raises_with_a_useful_message(self, monkeypatch):
        r = run(FakePath(gate_threshold_dbfs=-15.0), monkeypatch)
        with pytest.raises(NonLinearPath, match="gating, compressing or limiting"):
            require_linear_path(r)

    def test_message_includes_the_measured_table(self, monkeypatch):
        r = run(FakePath(gate_threshold_dbfs=-15.0), monkeypatch)
        with pytest.raises(NonLinearPath) as exc:
            require_linear_path(r)
        assert "spread across levels" in str(exc.value)

    def test_subtle_compression_is_still_caught(self, monkeypatch):
        # 3 dB is far too small to notice by eye on a response curve.
        r = run(FakePath(gate_threshold_dbfs=-15.0, gate_depth_db=3.0), monkeypatch)
        assert not r.is_linear


class TestSafetyInteraction:
    def test_skips_levels_above_the_channel_ceiling(self, monkeypatch):
        monkeypatch.setattr(qa, "play_record", FakePath())
        r = measure_level_linearity(
            sample_rate_hz=SR,
            output_channel=0,
            input_channel=1,
            limit=ChannelLimit(),  # uncharacterized: -20 dBFS ceiling
            duration_s=0.4,
            settle_s=0.1,
        )
        assert max(r.levels_dbfs) <= -20.0
        assert -6.0 not in r.levels_dbfs

    def test_rejects_a_ceiling_leaving_too_few_levels(self, monkeypatch):
        monkeypatch.setattr(qa, "play_record", FakePath())
        with pytest.raises(ValueError, match="at least 2 test levels"):
            measure_level_linearity(
                sample_rate_hz=SR,
                output_channel=0,
                input_channel=1,
                limit=ChannelLimit(ceiling_dbfs=-45.0, characterized=False),
            )


class TestResultShape:
    def test_grid_dimensions(self, monkeypatch):
        r = run(FakePath(), monkeypatch, freqs_hz=(500.0, 2000.0))
        assert r.gain_db.shape == (2, len(r.levels_dbfs))

    def test_report_is_readable(self, monkeypatch):
        text = run(FakePath(), monkeypatch).report()
        assert "level" in text and "spread across levels" in text

    def test_spread_is_across_levels_not_frequencies(self):
        # A path with a tilted but level-independent response is linear.
        result = LinearityResult(
            freqs_hz=(100.0, 1000.0),
            levels_dbfs=(-30.0, -10.0),
            gain_db=np.array([[-30.0, -30.0], [0.0, 0.0]]),
        )
        assert result.spread_db == 0.0
        assert result.is_linear


class NoisyCapture:
    """Capture path that returns a fixed noise floor regardless of stimulus.

    ``tone_hz`` injects a narrowband interferer so the band breakdown can be
    checked; ``hiss_dbfs`` sets a broadband floor.
    """

    def __init__(self, hiss_dbfs=-90.0, tone_hz=None, tone_dbfs=-60.0, seed=0):
        self.hiss_dbfs = hiss_dbfs
        self.tone_hz = tone_hz
        self.tone_dbfs = tone_dbfs
        self.seed = seed

    def __call__(
        self,
        stimulus,
        output_channel,
        input_channels,
        sample_rate_hz,
        device=None,
        loopback=None,
        tail_s=1.0,
        max_peak_dbfs=0.0,
    ):
        rng = np.random.default_rng(self.seed)
        n = stimulus.size
        y = rng.normal(0.0, 10 ** (self.hiss_dbfs / 20.0), n)
        if self.tone_hz is not None:
            t = np.arange(n) / sample_rate_hz
            y = y + 10 ** (self.tone_dbfs / 20.0) * np.sin(2 * np.pi * self.tone_hz * t)
        return y.reshape(-1, 1)


def idle(path, monkeypatch, **kw):
    monkeypatch.setattr(qa, "play_record", path)
    return qa.measure_idle_noise(
        sample_rate_hz=SR, input_channel=1, duration_s=0.5, **kw
    )


class TestIdleNoise:
    def test_quiet_path_passes(self, monkeypatch):
        result = idle(NoisyCapture(hiss_dbfs=-90.0), monkeypatch)
        qa.require_quiet_path(result, level_dbfs=-20.0)

    def test_loud_floor_raises(self, monkeypatch):
        result = idle(NoisyCapture(hiss_dbfs=-40.0), monkeypatch)
        with pytest.raises(qa.NoisyPath, match="only"):
            qa.require_quiet_path(result, level_dbfs=-20.0)

    def test_stimulus_is_silent(self, monkeypatch):
        """The check must not emit anything -- it measures the floor."""
        seen = {}

        def spy(stimulus, *a, **kw):
            seen["peak"] = float(np.max(np.abs(stimulus)))
            return np.zeros((stimulus.size, 1))

        monkeypatch.setattr(qa, "play_record", spy)
        qa.measure_idle_noise(sample_rate_hz=SR, input_channel=1, duration_s=0.2)
        assert seen["peak"] == 0.0

    def test_snr_is_relative_to_stimulus_level(self, monkeypatch):
        result = idle(NoisyCapture(hiss_dbfs=-70.0), monkeypatch)
        # Same floor passes against a loud stimulus and fails against a quiet one.
        qa.require_quiet_path(result, level_dbfs=-20.0, min_snr_db=40.0)
        with pytest.raises(qa.NoisyPath):
            qa.require_quiet_path(result, level_dbfs=-50.0, min_snr_db=40.0)

    def test_band_breakdown_locates_a_tone(self, monkeypatch):
        result = idle(
            NoisyCapture(hiss_dbfs=-120.0, tone_hz=1000.0, tone_dbfs=-40.0),
            monkeypatch,
        )
        bands = result.bands_dbfs
        assert bands[(150.0, 6000.0)] > bands[(20.0, 150.0)] + 30
        assert bands[(150.0, 6000.0)] > bands[(6000.0, 20000.0)] + 30

    def test_report_mentions_the_bands(self, monkeypatch):
        text = idle(NoisyCapture(), monkeypatch).report()
        assert "idle rms" in text and "20" in text


class TestLinearityOnFilteredChannels:
    """The check must not mistake a crossover's stopband for a compressor.

    Measured on the bench 2026-08-08: OUT5 is low-passed at 450 Hz, so the
    default 300/1000/3000 Hz tones put two of three deep into attenuation and
    3 kHz into the noise floor. The check reported 1.65 dB of "gain variation
    with level" and raised NonLinearPath on a path whose passband tones agreed
    to 0.02 dB. Every channel in a real car is filtered, so this would have
    fired on all of them.
    """

    def _result(self, gains_by_freq):
        freqs = tuple(float(f) for f in gains_by_freq)
        levels = (-40.0, -30.0, -20.0, -12.0, -6.0)
        gain = np.array([gains_by_freq[f] for f in gains_by_freq], dtype=float)
        return LinearityResult(freqs_hz=freqs, levels_dbfs=levels, gain_db=gain)

    def test_the_real_bench_data_now_passes(self):
        # Verbatim from the failing bench run.
        result = self._result(
            {
                300: [-1.49, -1.50, -1.48, -1.49, -1.50],
                1000: [-28.18, -28.15, -28.14, -28.13, -28.13],
                3000: [-65.51, -66.57, -66.99, -67.16, -67.15],
            }
        )
        assert result.spread_db_all_tones > 1.0  # what it used to judge on
        assert result.spread_db < 0.1  # what it judges on now
        require_linear_path(result)

    def test_the_noise_floor_tone_is_the_one_excluded(self):
        result = self._result(
            {
                300: [-1.49, -1.50, -1.48, -1.49, -1.50],
                1000: [-28.18, -28.15, -28.14, -28.13, -28.13],
                3000: [-65.51, -66.57, -66.99, -67.16, -67.15],
            }
        )
        assert list(result.usable()) == [True, True, False]
        assert "3000 Hz" in result.report()
        assert "excluded as noise floor" in result.report()

    def test_a_real_compressor_in_the_passband_still_raises(self):
        # Excluding stopband tones must not blunt the check where it matters.
        result = self._result(
            {
                300: [-1.49, -1.50, -1.48, -1.49, -1.50],
                1000: [-30.0, -29.0, -28.5, -28.2, -28.1],
            }
        )
        with pytest.raises(NonLinearPath):
            require_linear_path(result)

    def test_too_few_usable_tones_is_indeterminate_not_a_pass(self):
        # A tweeter channel can leave one tone in band. One point cannot
        # establish level-independence, only fail to contradict it.
        result = self._result(
            {
                300: [-1.49, -1.50, -1.48, -1.49, -1.50],
                1000: [-70.1, -70.4, -70.9, -71.0, -71.0],
                3000: [-72.5, -72.9, -73.1, -73.0, -73.2],
            }
        )
        assert int(result.usable().sum()) == 1
        with pytest.raises(IndeterminateLinearity, match="passband"):
            require_linear_path(result)

    def test_indeterminate_is_not_a_subclass_of_nonlinear(self):
        # Three outcomes means a caller can tell them apart.
        assert not issubclass(IndeterminateLinearity, NonLinearPath)
        assert not issubclass(NonLinearPath, IndeterminateLinearity)
