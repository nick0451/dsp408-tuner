"""The PEQ bench tool's analysis, checked before it meets hardware.

``tools/bench_peq.py`` decides whether the device's bandwidth convention
matches ours. If its own fitter or its convention arithmetic is wrong, it will
produce a confident verdict about the firmware that is really a statement about
our arithmetic -- and we would act on it, because the whole point of running it
is that we do not already know the answer.

So the analysis path is exercised here on synthetic filters whose parameters
are known exactly, with no audio hardware involved. The bench session is not
the first time this code runs.

The tool is a script rather than a package module, so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from tuner.dsp.protocol import bandwidth_octaves, q_from_bw_raw
from tuner.measure import log_freqs

TOOL = Path(__file__).resolve().parents[1] / "tools" / "bench_peq.py"


def _load():
    spec = importlib.util.spec_from_file_location("bench_peq", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load()

#: bw_raw values spanning the useful range: narrow, mid, wide.
BW_RAWS = [25, 106, 195]


def _synthetic(bw_raw: int, gain_db: float, f0: float = 1000.0, offset: float = 3.7):
    """An exact RBJ band plus a channel offset, as the rig would see it."""
    q = q_from_bw_raw(bw_raw)
    freqs = log_freqs(f0 / 16.0, f0 * 16.0, 400)
    return freqs, bench.peaking_db(freqs, f0, q, gain_db) + offset, q


class TestFitRecoversKnownFilters:
    @pytest.mark.parametrize("bw_raw", BW_RAWS)
    @pytest.mark.parametrize("gain_db", [12.0, -12.0, 9.0])
    def test_parameters_are_recovered_exactly(self, bw_raw, gain_db):
        freqs, curve, q = _synthetic(bw_raw, gain_db)
        # Deliberately poor starting guesses: the bench operator's "what was
        # set in the app" may itself be wrong, which is partly what the tool
        # is for.
        f0, q_fit, gain_fit, offset, rms = bench.fit_peaking(
            freqs, curve, 700.0, 1.0, gain_db / 2
        )
        assert f0 == pytest.approx(1000.0, rel=1e-3)
        assert q_fit == pytest.approx(q, rel=1e-3)
        assert gain_fit == pytest.approx(gain_db, abs=0.02)
        assert offset == pytest.approx(3.7, abs=0.02)
        assert rms < 1e-6

    def test_a_shelf_fits_well_but_degenerately(self):
        # A low residual is NOT on its own evidence that the shape is right.
        # A peaking section with an absurdly low Q flattens into a broad tilt
        # and fits almost anything smooth: this shelf fits to ~0.13 dB rms at
        # Q ~ 0.03. Discovered by this test failing an earlier, wrong
        # assertion that the residual would be large.
        #
        # What actually distinguishes the cases is that the device cannot
        # store such a filter, which is why the tool checks the fitted Q
        # against a plausible range instead of trusting the residual.
        freqs = log_freqs(60.0, 16_000.0, 400)
        shelf = 12.0 / (1.0 + (300.0 / freqs) ** 2)
        _, q_fit, _, _, rms = bench.fit_peaking(freqs, shelf, 1000.0, 1.0, 12.0)
        assert rms < 1.0, "the residual alone does not catch this"

        q_lo, q_hi = bench._plausible_q_range()
        assert not (q_lo <= q_fit <= q_hi), "the plausibility guard must catch it"

    @pytest.mark.parametrize("bw_raw", BW_RAWS)
    def test_a_genuine_band_fits_inside_the_plausible_range(self, bw_raw):
        # The converse: the guard must not fire on real filters.
        freqs, curve, _ = _synthetic(bw_raw, 12.0)
        _, q_fit, _, _, _ = bench.fit_peaking(freqs, curve, 900.0, 1.0, 6.0)
        q_lo, q_hi = bench._plausible_q_range()
        assert q_lo <= q_fit <= q_hi


class TestBandwidthConventions:
    """The discrimination the whole experiment depends on."""

    @pytest.mark.parametrize("bw_raw", BW_RAWS)
    def test_half_gain_width_reproduces_the_requested_octaves(self, bw_raw):
        # Our model's own self-consistency: a filter built from bw_raw must
        # measure that many octaves wide at its half-gain points. Residual
        # error is bilinear-transform warping, not a modelling choice.
        q = q_from_bw_raw(bw_raw)
        want = bandwidth_octaves(bw_raw)
        got = bench.bandwidth_octaves_at(1000.0, q, 12.0, 6.0)
        assert got == pytest.approx(want, abs=0.02)

    @pytest.mark.parametrize("bw_raw", BW_RAWS)
    def test_the_two_conventions_are_identical_at_six_db(self, bw_raw):
        # Why the tool refuses to draw a conclusion at 6 dB. Half-gain is
        # G/2 = 3; minus-three-dB is G-3 = 3. The same number.
        q = q_from_bw_raw(bw_raw)
        half = bench.bandwidth_octaves_at(1000.0, q, 6.0, 3.0)
        minus3 = bench.bandwidth_octaves_at(1000.0, q, 6.0, 3.0)
        assert half == minus3

    @pytest.mark.parametrize("bw_raw", BW_RAWS)
    def test_the_two_conventions_separate_at_twelve_db(self, bw_raw):
        q = q_from_bw_raw(bw_raw)
        half = bench.bandwidth_octaves_at(1000.0, q, 12.0, 6.0)
        minus3 = bench.bandwidth_octaves_at(1000.0, q, 12.0, 9.0)
        assert minus3 < half
        assert half - minus3 > 0.1

    def test_the_separation_grows_with_bandwidth(self):
        # This is why one bandwidth is not enough: a narrow band's two
        # conventions differ by ~0.14 octaves, which a noisy fit could miss.
        # A wide band's differ by ~0.9, which nothing could miss.
        gaps = []
        for bw_raw in BW_RAWS:
            q = q_from_bw_raw(bw_raw)
            half = bench.bandwidth_octaves_at(1000.0, q, 12.0, 6.0)
            minus3 = bench.bandwidth_octaves_at(1000.0, q, 12.0, 9.0)
            gaps.append(half - minus3)
        assert gaps == sorted(gaps), "separation should widen with bandwidth"
        assert gaps[0] < 0.25 < gaps[-1]

    def test_a_cut_is_handled_as_well_as_a_boost(self):
        q = q_from_bw_raw(106)
        want = bandwidth_octaves(106)
        got = bench.bandwidth_octaves_at(1000.0, q, -12.0, -6.0)
        assert got == pytest.approx(want, abs=0.02)

    def test_an_uncrossable_level_returns_none(self):
        # Asking for the width at a level the filter never reaches must not
        # invent a number.
        q = q_from_bw_raw(106)
        assert bench.bandwidth_octaves_at(1000.0, q, 6.0, 99.0) is None

    def test_a_flat_band_has_no_width(self):
        assert bench.bandwidth_octaves_at(1000.0, 1.0, 0.0, 0.0) is None


class TestEndToEndVerdict:
    """A full synthetic run, to prove the pieces compose."""

    @pytest.mark.parametrize("bw_raw", BW_RAWS)
    def test_a_device_obeying_our_model_is_recognised(self, bw_raw):
        freqs, curve, q = _synthetic(bw_raw, 12.0)
        f0, q_fit, gain_fit, _, rms = bench.fit_peaking(freqs, curve, 900.0, 1.0, 6.0)
        assert rms < 1e-6
        want = bandwidth_octaves(bw_raw)
        half = bench.bandwidth_octaves_at(f0, q_fit, gain_fit, gain_fit / 2)
        assert abs(half - want) <= 0.05 * want

    @pytest.mark.parametrize("bw_raw", [106, 195])
    def test_a_device_using_the_other_convention_is_distinguishable(self, bw_raw):
        # Simulate firmware that reads N as a -3 dB width: it would pick a
        # narrower Q than we predict. The tool must not mistake that for a
        # match.
        want = bandwidth_octaves(bw_raw)
        wrong_q = None
        for candidate in np.linspace(0.2, 12.0, 4000):
            width = bench.bandwidth_octaves_at(1000.0, float(candidate), 12.0, 9.0)
            if width is not None and width <= want:
                wrong_q = float(candidate)
                break
        assert wrong_q is not None

        freqs = log_freqs(1000 / 16, 1000 * 16, 400)
        curve = bench.peaking_db(freqs, 1000.0, wrong_q, 12.0)
        f0, q_fit, gain_fit, _, rms = bench.fit_peaking(freqs, curve, 900.0, 1.0, 6.0)
        assert rms < 1e-6

        half = bench.bandwidth_octaves_at(f0, q_fit, gain_fit, gain_fit / 2)
        minus3 = bench.bandwidth_octaves_at(f0, q_fit, gain_fit, gain_fit - 3.0)
        # The -3 dB reading matches the request; the half-gain one does not.
        assert abs(minus3 - want) < abs(half - want)
        assert abs(half - want) > 0.05 * want


class TestDifferentialMethod:
    """Two sweeps, and everything except the filter cancels.

    The single-sweep fit has to absorb the speaker, room, microphone and
    interface into one flat offset term, which only works if all of them are
    flat. None of them is. Taking a reference sweep with the band flat and
    dividing removes them exactly, because they are identical in both sweeps.

    It is also what makes this measurable **without removing the DSP from the
    car** -- the car's own response is one of the things that cancels.
    """

    @staticmethod
    def _hostile_system(freqs):
        """A response nothing could mistake for flat."""
        return (
            -12 * np.log10(np.maximum(freqs, 1.0) / 1000.0)  # broadband tilt
            + 8 * np.exp(-(((np.log(freqs) - np.log(180.0)) / 0.15) ** 2))  # mode
            - 20 * np.log10(1 + (80.0 / freqs) ** 4)  # LF rolloff
            - 20 * np.log10(1 + (freqs / 9000.0) ** 4)  # HF rolloff
        )

    @pytest.mark.parametrize("bw_raw", BW_RAWS)
    def test_it_recovers_the_filter_through_an_arbitrary_system(self, bw_raw):
        q = q_from_bw_raw(bw_raw)
        freqs = log_freqs(1000 / 16, 1000 * 16, 400)
        system = self._hostile_system(freqs)

        reference = system  # sweep 1: band flat
        measured = system + bench.peaking_db(freqs, 1000.0, q, 12.0)
        differential = measured - reference

        f0, q_fit, gain, _, rms = bench.fit_peaking(
            freqs, differential, 900.0, 1.0, 6.0
        )
        assert rms < 1e-6
        assert f0 == pytest.approx(1000.0, rel=1e-4)
        assert q_fit == pytest.approx(q, rel=1e-4)
        assert gain == pytest.approx(12.0, abs=0.01)

    def test_the_single_sweep_fit_is_confounded_by_the_same_system(self):
        # Why the differential is worth the second sweep. The system's own
        # shape lands in the fit and corrupts the answer.
        q = q_from_bw_raw(65)
        freqs = log_freqs(1000 / 16, 1000 * 16, 400)
        measured = self._hostile_system(freqs) + bench.peaking_db(
            freqs, 1000.0, q, 12.0
        )
        _, q_fit, _, _, rms = bench.fit_peaking(freqs, measured, 900.0, 1.0, 6.0)
        assert rms > 1.0, "a single sweep should fit this badly"
        assert abs(q_fit - q) / q > 0.05, "and get Q wrong"

    def test_out_of_band_residual_detects_drift_between_sweeps(self):
        # The guard the tool prints. If anything moved between sweeps -- the
        # mic, a gain, the wrong band edited -- the difference will not be
        # ~0 dB away from the band, and the fit must not be trusted.
        freqs = log_freqs(1000 / 16, 1000 * 16, 400)
        q = q_from_bw_raw(65)
        band = bench.peaking_db(freqs, 1000.0, q, 12.0)

        clean = band
        out_of_band = clean[(freqs < 125.0) | (freqs > 8000.0)]
        assert np.max(np.abs(out_of_band)) < 0.5

        drifted = band + 1.5  # e.g. a level changed between sweeps
        out_of_band = drifted[(freqs < 125.0) | (freqs > 8000.0)]
        assert np.max(np.abs(out_of_band)) > 0.5
