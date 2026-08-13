"""Known-answer tests for the measurement engine.

No REW reference data exists yet, so validation here is against systems whose
response is known *analytically*: a pure delay, a designed biquad, a synthetic
exponential decay. That is a stronger check than diffing against another tool,
because the right answer is exact rather than another implementation's opinion.

The biquad-recovery test is the load-bearing one. It exercises sweep
generation, deconvolution, windowing and the frequency-response path together,
and a deconvolution bug cannot survive it -- which matters because such a bug
otherwise produces a smooth, plausible, entirely wrong curve.
"""

from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest
from scipy import signal

from tuner.measure import (
    Coupling,
    IncomparableProvenance,
    Measurement,
    Provenance,
    arrival_offset_samples,
    deconvolve,
    frequency_response,
    gate,
    group_delay_samples,
    log_freqs,
    log_sweep,
    magnitude_db,
    octave_bands,
    peak_index,
    phase_rad,
    rt60_from_impulse,
    spatial_average,
    valid_above_hz,
)
from tuner.measure.timing import TimingReference

SR = 48_000


@pytest.fixture(scope="module")
def sweep():
    return log_sweep(20.0, 20_000.0, duration_s=1.0, sample_rate_hz=SR)


def with_tail(x: np.ndarray, seconds: float = 0.3) -> np.ndarray:
    """Append silence so a system's decay is not truncated by the capture."""
    return np.concatenate([x, np.zeros(int(seconds * SR))])


def provenance(
    sample_rate_hz: int = SR,
    temperature_c: float = 20.0,
    setup_token: str = "synthetic, unmoved",
) -> Provenance:
    return Provenance(
        device="synthetic",
        sample_rate_hz=sample_rate_hz,
        gains_db=(0.0,),
        timestamp=datetime(2026, 1, 1, 12, 0),
        cal_sha256="test",
        temperature_c=temperature_c,
        setup_token=setup_token,
    )


class TestSweepGeneration:
    def test_length_and_peak(self, sweep):
        assert sweep.samples.size == SR
        assert np.max(np.abs(sweep.samples)) == pytest.approx(1.0, abs=1e-6)

    def test_starts_and_ends_near_silence(self, sweep):
        # The raised-cosine fades exist to suppress a broadband click.
        assert abs(sweep.samples[0]) < 1e-6
        assert abs(sweep.samples[-1]) < 1e-6

    def test_instantaneous_frequency_rises(self, sweep):
        # Zero crossings should get denser through the sweep.
        crossings = np.flatnonzero(np.diff(np.signbit(sweep.samples)))
        early = np.diff(crossings[:20]).mean()
        late = np.diff(crossings[-20:]).mean()
        assert late < early / 10

    @pytest.mark.parametrize(
        ("start", "stop", "duration", "rate"),
        [
            (0.0, 20_000.0, 1.0, SR),  # non-positive start
            (20_000.0, 20.0, 1.0, SR),  # inverted range
            (20.0, 20_000.0, 0.0, SR),  # zero duration
            (20.0, 30_000.0, 1.0, SR),  # above Nyquist
        ],
    )
    def test_rejects_invalid_parameters(self, start, stop, duration, rate):
        with pytest.raises(ValueError):
            log_sweep(start, stop, duration, rate)


class TestDeconvolution:
    def test_perfect_system_yields_flat_0db(self, sweep):
        # The invariant is a flat *response*, not a unit peak sample. The
        # deconvolved impulse is band-limited, so its peak differs from its
        # passband gain by fs/2(f2-f1) -- normalizing the peak would offset
        # every magnitude measurement by ~1.6 dB.
        ir = deconvolve(sweep.samples, sweep.inverse)
        assert peak_index(ir) == sweep.t_zero_index

        freqs = log_freqs(200.0, 15_000.0, 200)
        assert np.max(np.abs(magnitude_db(ir, SR, freqs))) < 0.1

    def test_response_degrades_toward_the_sweep_edges(self, sweep):
        # Documented behaviour, not a defect: within an octave of the sweep
        # endpoints the response ripples, and outside them it rolls off. This
        # is why callers must sweep wider than their band of interest.
        ir = deconvolve(sweep.samples, sweep.inverse)
        interior = magnitude_db(ir, SR, log_freqs(200.0, 15_000.0, 100))
        edge = magnitude_db(ir, SR, log_freqs(20.0, 30.0, 20))

        assert np.max(np.abs(interior)) < 0.1
        assert np.max(np.abs(edge)) > 1.0

    def test_impulse_is_concentrated(self, sweep):
        # A correct deconvolution puts nearly all energy in a few samples.
        ir = deconvolve(sweep.samples, sweep.inverse)
        t0 = sweep.t_zero_index
        core = ir[t0 - 20 : t0 + 20]
        assert np.sum(core**2) / np.sum(ir**2) > 0.95

    @pytest.mark.parametrize("delay", [0, 1, 37, 480, 4801])
    def test_recovers_known_delay_exactly(self, sweep, delay):
        # The loopback round-trip check from the verification plan: a delay
        # inserted digitally must come back exactly, in samples.
        captured = np.concatenate([np.zeros(delay), with_tail(sweep.samples)])
        ir = deconvolve(captured, sweep.inverse)
        assert peak_index(ir) - sweep.t_zero_index == delay

    def test_rejects_multichannel_input(self, sweep):
        with pytest.raises(ValueError, match="1-D"):
            deconvolve(np.zeros((100, 2)), sweep.inverse)

    def test_rejects_empty_input(self, sweep):
        with pytest.raises(ValueError, match="non-empty"):
            deconvolve(np.array([]), sweep.inverse)


class TestKnownSystemRecovery:
    """End-to-end: push a sweep through a known filter, recover its response."""

    @staticmethod
    def measure_through(sos_or_ba, sweep, ir_seconds=0.5):
        b, a = sos_or_ba
        captured = signal.lfilter(b, a, with_tail(sweep.samples))
        ir = deconvolve(captured, sweep.inverse)
        t0 = sweep.t_zero_index
        return ir[t0 : t0 + int(ir_seconds * SR)]

    def test_recovers_lowpass_magnitude(self, sweep):
        b, a = signal.butter(2, 2000.0, btype="low", fs=SR)
        ir = self.measure_through((b, a), sweep)

        freqs = log_freqs(50.0, 10_000.0, 200)
        measured = magnitude_db(ir, SR, freqs)
        _, h = signal.freqz(b, a, worN=freqs, fs=SR)
        expected = 20 * np.log10(np.abs(h))

        assert np.max(np.abs(measured - expected)) < 0.5

    def test_recovers_peaking_filter_magnitude(self, sweep):
        # A resonant peak is the shape PEQ fitting will actually chase.
        b, a = signal.iirpeak(1000.0, Q=4.0, fs=SR)
        ir = self.measure_through((b, a), sweep)

        freqs = log_freqs(100.0, 10_000.0, 200)
        measured = magnitude_db(ir, SR, freqs)
        _, h = signal.freqz(b, a, worN=freqs, fs=SR)
        expected = 20 * np.log10(np.abs(h))

        assert np.max(np.abs(measured - expected)) < 0.5

    def test_recovers_linear_phase_of_pure_delay(self, sweep):
        delay = 100
        captured = np.concatenate([np.zeros(delay), with_tail(sweep.samples)])
        ir = deconvolve(captured, sweep.inverse)
        t0 = sweep.t_zero_index
        trimmed = ir[t0 : t0 + 4096]

        freqs = log_freqs(200.0, 10_000.0, 100)
        measured = phase_rad(trimmed, SR, freqs)
        expected = -2 * np.pi * freqs * delay / SR

        assert np.max(np.abs(measured - expected)) < 0.05


class TestPhaseUnwrapping:
    """Regression tests for a bug found during development.

    ``np.unwrap`` assumes adjacent points differ by less than pi. On a
    log-spaced axis the sparse high end violates that, and it fails *silently*
    -- returning a smooth, plausible, wrong curve. Since every frequency axis
    here is log-spaced, the naive approach is wrong everywhere it matters.
    """

    @pytest.mark.parametrize("delay", [10, 500, 2000])
    def test_long_delays_unwrap_correctly_on_a_log_axis(self, delay):
        impulse = np.zeros(4096)
        impulse[delay] = 1.0
        freqs = log_freqs(100.0, 20_000.0, 128)

        measured = phase_rad(impulse, SR, freqs)
        expected = -2 * np.pi * freqs * delay / SR

        assert np.max(np.abs(measured - expected)) < 0.05

    def test_naive_unwrap_on_a_log_axis_would_be_wrong(self):
        # Documents the trap rather than testing our code: if this ever starts
        # agreeing, the axis is dense enough that the guard is moot.
        delay = 2000
        impulse = np.zeros(4096)
        impulse[delay] = 1.0
        freqs = log_freqs(100.0, 20_000.0, 128)

        naive = np.unwrap(np.angle(frequency_response(impulse, SR, freqs)))
        correct = phase_rad(impulse, SR, freqs)

        assert np.max(np.abs(naive - correct)) > 100.0

    def test_unwrap_can_be_disabled(self):
        impulse = np.zeros(4096)
        impulse[2000] = 1.0
        freqs = log_freqs(100.0, 20_000.0, 64)
        wrapped = phase_rad(impulse, SR, freqs, unwrap=False)
        assert np.all(np.abs(wrapped) <= np.pi + 1e-9)


class TestGroupDelay:
    def test_recovers_constant_delay(self):
        delay = 240
        impulse = np.zeros(4096)
        impulse[delay] = 1.0
        freqs = log_freqs(500.0, 10_000.0, 64)
        measured = group_delay_samples(impulse, SR, freqs)
        assert np.median(measured) == pytest.approx(delay, rel=0.02)


class TestGating:
    def test_valid_above_matches_window(self):
        assert valid_above_hz(5.0) == pytest.approx(200.0)
        assert valid_above_hz(10.0) == pytest.approx(100.0)

    def test_returns_requested_length(self):
        out = gate(np.ones(10_000), SR, window_ms=5.0)
        assert out.size == int(round(5.0 * SR / 1000.0))

    def test_zero_pads_when_data_runs_out(self):
        out = gate(np.ones(10), SR, window_ms=5.0)
        assert out.size == 240
        assert out[-1] == 0.0

    def test_onset_is_not_tapered(self):
        # Fading the onset would attenuate exactly the HF the gate preserves.
        out = gate(np.ones(10_000), SR, window_ms=5.0)
        assert out[0] == pytest.approx(1.0)

    def test_trailing_edge_is_tapered(self):
        out = gate(np.ones(10_000), SR, window_ms=5.0)
        assert out[-1] == pytest.approx(0.0, abs=1e-9)

    def test_respects_arrival_offset(self):
        impulse = np.zeros(10_000)
        impulse[500] = 1.0
        out = gate(impulse, SR, window_ms=5.0, arrival_samples=500)
        assert out[0] == pytest.approx(1.0)

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_rejects_non_positive_window(self, bad):
        with pytest.raises(ValueError):
            gate(np.ones(100), SR, window_ms=bad)

    def test_gating_barely_changes_a_clean_impulse(self, sweep):
        b, a = signal.butter(2, 2000.0, btype="low", fs=SR)
        captured = signal.lfilter(b, a, with_tail(sweep.samples))
        ir = deconvolve(captured, sweep.inverse)
        t0 = sweep.t_zero_index
        full = ir[t0 : t0 + 24_000]
        gated = gate(ir, SR, window_ms=100.0, arrival_samples=t0)

        freqs = log_freqs(200.0, 10_000.0, 100)
        diff = magnitude_db(gated, SR, freqs) - magnitude_db(full, SR, freqs)
        assert np.max(np.abs(diff)) < 0.5


class TestRt60:
    @staticmethod
    def synth_decay(t60_s: float, seconds: float = 2.0, seed: int = 0):
        rng = np.random.default_rng(seed)
        n = int(seconds * SR)
        tau = t60_s * SR / (3.0 * np.log(10.0))
        return rng.standard_normal(n) * np.exp(-np.arange(n) / tau)

    @pytest.mark.parametrize("t60", [0.3, 0.6])
    def test_recovers_synthetic_decay(self, t60):
        impulse = self.synth_decay(t60)
        bands, times = rt60_from_impulse(
            impulse, SR, bands_hz=np.array([1000.0]), method="t20"
        )
        assert bands[0] == 1000.0
        assert times[0] == pytest.approx(t60, rel=0.10)

    def test_t30_agrees_with_t20(self):
        impulse = self.synth_decay(0.5)
        _, t20 = rt60_from_impulse(impulse, SR, bands_hz=np.array([1000.0]))
        _, t30 = rt60_from_impulse(
            impulse, SR, bands_hz=np.array([1000.0]), method="t30"
        )
        assert t20[0] == pytest.approx(t30[0], rel=0.10)

    def test_silence_yields_nan_not_a_number(self):
        # An unmeasurable decay must be honest rather than extrapolated.
        _, times = rt60_from_impulse(np.zeros(SR), SR, bands_hz=np.array([1000.0]))
        assert np.isnan(times[0])

    def test_rejects_unknown_method(self):
        with pytest.raises(ValueError, match="method must be"):
            rt60_from_impulse(np.zeros(100), SR, method="t60")

    def test_default_bands_are_powers_of_two_from_1k(self):
        bands = octave_bands()
        assert 1000.0 in bands
        assert bands[0] == pytest.approx(31.25)
        ratios = bands[1:] / bands[:-1]
        assert np.allclose(ratios, 2.0)


class TestSpatialAverage:
    @staticmethod
    def make(delay: int, has_ref: bool, sample_rate_hz: int = SR) -> Measurement:
        impulse = np.zeros(4096)
        impulse[delay] = 1.0
        return Measurement(
            impulse=impulse,
            provenance=provenance(sample_rate_hz=sample_rate_hz),
            arrival_samples=delay if has_ref else None,
            timing=(TimingReference.LOOPBACK if has_ref else TimingReference.NONE),
        )

    def test_complex_average_shows_cancellation(self):
        # Two unit impulses offset in time cancel at frequencies where they
        # are out of phase. Complex averaging must show that.
        freqs = np.array([SR / 200.0])  # half-period = 100 samples
        avg = spatial_average([self.make(0, True), self.make(100, True)], freqs)
        assert avg[0] < -40

    def test_power_average_hides_cancellation(self):
        # Without a timing reference the same pair averages to 0 dB, because
        # magnitude averaging cannot see the phase opposition.
        freqs = np.array([SR / 200.0])
        avg = spatial_average([self.make(0, False), self.make(100, False)], freqs)
        assert avg[0] == pytest.approx(0.0, abs=0.01)

    def test_rejects_mixed_timing_reference(self):
        with pytest.raises(ValueError, match="timing reference"):
            spatial_average(
                [self.make(0, True), self.make(10, False)], log_freqs(100, 1000, 10)
            )

    def test_rejects_mismatched_sample_rates(self):
        with pytest.raises(ValueError, match="sample rates"):
            spatial_average(
                [self.make(0, True), self.make(0, True, sample_rate_hz=44_100)],
                log_freqs(100, 1000, 10),
            )

    def test_rejects_empty_list(self):
        with pytest.raises(ValueError, match="at least one"):
            spatial_average([], log_freqs(100, 1000, 10))


class TestFrequencyResponseGuards:
    def test_rejects_frequencies_at_or_above_nyquist(self):
        with pytest.raises(ValueError, match="Nyquist"):
            frequency_response(np.ones(10), SR, np.array([24_000.0]))

    def test_rejects_non_positive_frequencies(self):
        with pytest.raises(ValueError, match="positive"):
            frequency_response(np.ones(10), SR, np.array([0.0]))


class TestMeasurementAccessors:
    def test_magnitude_available_without_timing_reference(self):
        impulse = np.zeros(1024)
        impulse[0] = 1.0
        m = Measurement(impulse=impulse, provenance=provenance())
        mag = m.magnitude_dbfs(log_freqs(100, 10_000, 50))
        assert np.allclose(mag, 0.0, atol=1e-6)

    def test_phase_still_refuses_without_timing_reference(self):
        # The rule survives having a real implementation behind it.
        from tuner.measure import NoTimingReference

        m = Measurement(impulse=np.ones(64), provenance=provenance())
        with pytest.raises(NoTimingReference):
            m.phase_rad(log_freqs(100, 1000, 10))

    def test_phase_available_with_timing_reference(self):
        impulse = np.zeros(1024)
        impulse[10] = 1.0
        m = Measurement(
            impulse=impulse,
            provenance=provenance(),
            arrival_samples=10,
            timing=TimingReference.LOOPBACK,
        )
        freqs = log_freqs(100, 10_000, 50)
        expected = -2 * np.pi * freqs * 10 / SR
        assert np.max(np.abs(m.phase_rad(freqs) - expected)) < 1e-6

    def test_group_delay_refuses_without_timing_reference(self):
        # CLAUDE.md's timing-reference rule names delay, phase AND group
        # delay. Group delay had no guarded accessor at all -- it existed only
        # as a free function taking a bare impulse array, so the rule was
        # enforceable on two of the three quantities it names.
        from tuner.measure import NoTimingReference

        m = Measurement(impulse=np.ones(64), provenance=provenance())
        with pytest.raises(NoTimingReference, match="group delay"):
            m.group_delay_samples(log_freqs(100, 1000, 10))

    def test_group_delay_available_with_timing_reference(self):
        impulse = np.zeros(1024)
        impulse[10] = 1.0
        m = Measurement(
            impulse=impulse,
            provenance=provenance(),
            arrival_samples=10,
            timing=TimingReference.LOOPBACK,
        )
        # A pure delay has constant group delay equal to that delay.
        got = m.group_delay_samples(log_freqs(200, 5_000, 40))
        assert np.max(np.abs(got - 10.0)) < 1e-3

    def test_a_measurement_without_a_reference_still_has_an_arrival_index(self):
        # The trap the guard closes: arrival_samples can be populated while
        # has_timing_reference is False, because deconvolution always finds a
        # peak. The number looks usable and is offset by unknown interface
        # latency. Both conditions must be checked, not either.
        from tuner.measure import NoTimingReference

        impulse = np.zeros(256)
        impulse[42] = 1.0
        m = Measurement(
            impulse=impulse,
            provenance=provenance(),
            arrival_samples=42,
            timing=TimingReference.NONE,
        )
        for call in (
            lambda: m.delay_samples(),
            lambda: m.phase_rad(log_freqs(100, 1000, 10)),
            lambda: m.group_delay_samples(log_freqs(100, 1000, 10)),
        ):
            with pytest.raises(NoTimingReference):
                call()


class TestProvenanceRefusal:
    """``require_comparable`` must raise, not warn and return.

    ``verify.py``'s indeterminate path was well covered; this raise branch was
    not covered at all, and it is the one that guards ad-hoc comparisons made
    outside the optimizer. Every branch of ``comparable_to`` gets a case,
    because each is a different way for a measurement to look diffable when it
    is not.
    """

    @staticmethod
    def _measurement(prov: Provenance) -> Measurement:
        impulse = np.zeros(256)
        impulse[0] = 1.0
        return Measurement(impulse=impulse, provenance=prov)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("device", "a different interface"),
            ("sample_rate_hz", 44_100),
            ("gains_db", (12.0,)),
            ("cal_sha256", "a different cal file"),
        ],
    )
    def test_refuses_to_diff_across_a_changed_condition(self, field, value):
        base = provenance()
        assert getattr(base, field) != value, "test must actually change it"
        a = self._measurement(base)
        b = self._measurement(replace(base, **{field: value}))
        with pytest.raises(IncomparableProvenance):
            a.require_comparable(b)

    def test_refuses_when_temperature_is_missing(self):
        # Not "assume it was the same". A car's response moves with
        # temperature, so an absent reading is missing evidence, not a
        # default -- and this is the case most likely to arise from an
        # older capture rather than from a mistake.
        a = self._measurement(provenance())
        b = self._measurement(replace(provenance(), temperature_c=None))
        with pytest.raises(IncomparableProvenance):
            a.require_comparable(b)
        with pytest.raises(IncomparableProvenance):
            b.require_comparable(a)

    def test_refuses_on_temperature_drift(self):
        a = self._measurement(provenance(temperature_c=20.0))
        b = self._measurement(provenance(temperature_c=23.0))
        with pytest.raises(IncomparableProvenance):
            a.require_comparable(b)

    def test_allows_a_drift_inside_tolerance(self):
        a = self._measurement(provenance(temperature_c=20.0))
        b = self._measurement(provenance(temperature_c=21.5))
        a.require_comparable(b)  # must not raise

    def test_refusal_is_symmetric(self):
        a = self._measurement(provenance())
        b = self._measurement(replace(provenance(), device="other"))
        with pytest.raises(IncomparableProvenance):
            a.require_comparable(b)
        with pytest.raises(IncomparableProvenance):
            b.require_comparable(a)


class TestArrivalOffset:
    def test_measures_difference_between_peaks(self):
        ref = np.zeros(1000)
        ref[100] = 1.0
        mic = np.zeros(1000)
        mic[340] = 1.0
        assert arrival_offset_samples(mic, ref) == 240

    def test_finds_inverted_polarity_peak(self):
        ref = np.zeros(1000)
        ref[100] = 1.0
        mic = np.zeros(1000)
        mic[340] = -1.0
        assert arrival_offset_samples(mic, ref) == 240


class TestCouplingDecidesComparability:
    """Temperature is an acoustic term, and an electrical path has no acoustics.

    `comparable_to` previously required a temperature on **every** measurement.
    That made an electrical bench comparison impossible to satisfy honestly:
    DSP RCA output straight into a line input has no propagation path, no room
    and no microphone, so the only way to pass was to record a room temperature
    for a cable -- asserting the relevance of a variable that has none.

    Raised by the operator on 2026-08-12, against a proposal to do exactly
    that in order to make VERIFY go green.
    """

    def _prov(self, **kw):
        base = dict(
            device="iface",
            sample_rate_hz=44_100,
            gains_db=(0.0,),
            timestamp=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
        )
        base.update(kw)
        return Provenance(**base)

    def test_electrical_measurements_compare_without_a_temperature(self):
        a = self._prov(coupling=Coupling.ELECTRICAL)
        b = self._prov(coupling=Coupling.ELECTRICAL)
        assert a.temperature_c is None
        assert a.comparable_to(b)

    def test_acoustic_measurements_still_require_one(self):
        a = self._prov(coupling=Coupling.ACOUSTIC)
        b = self._prov(coupling=Coupling.ACOUSTIC)
        assert not a.comparable_to(b)

    def test_the_default_is_the_strict_reading(self):
        # A measurement that never says gets treated as though a room were
        # involved. Loosening must be a declaration, never an omission.
        assert Provenance.__dataclass_fields__["coupling"].default is Coupling.ACOUSTIC

    def test_electrical_and_acoustic_are_never_comparable_to_each_other(self):
        a = self._prov(coupling=Coupling.ELECTRICAL)
        b = self._prov(coupling=Coupling.ACOUSTIC, temperature_c=21.0)
        assert not a.comparable_to(b)
        assert not b.comparable_to(a)

    def test_the_signal_chain_terms_still_apply_to_electrical(self):
        # Dropping the temperature requirement must not drop the checks that
        # do apply. A different preamp gain still makes two curves
        # incomparable, cable or no cable.
        a = self._prov(coupling=Coupling.ELECTRICAL)
        assert not a.comparable_to(
            self._prov(coupling=Coupling.ELECTRICAL, gains_db=(6.0,))
        )
        assert not a.comparable_to(
            self._prov(coupling=Coupling.ELECTRICAL, device="other")
        )
        assert not a.comparable_to(
            self._prov(coupling=Coupling.ELECTRICAL, sample_rate_hz=48_000)
        )
        assert not a.comparable_to(
            self._prov(coupling=Coupling.ELECTRICAL, cal_sha256="deadbeef")
        )

    def test_acoustic_still_honours_the_tolerance(self):
        a = self._prov(
            coupling=Coupling.ACOUSTIC, temperature_c=21.0, setup_token="unmoved"
        )
        assert a.comparable_to(
            self._prov(
                coupling=Coupling.ACOUSTIC, temperature_c=22.5, setup_token="unmoved"
            )
        )
        assert not a.comparable_to(
            self._prov(
                coupling=Coupling.ACOUSTIC, temperature_c=25.0, setup_token="unmoved"
            )
        )


class TestTheSetupToken:
    """The operator's claim that the physical configuration is unchanged.

    Temperature was the only environmental term this project gated on, and it
    is the *weakest* one. Microphone position, seat position, doors, windows,
    HVAC and occupancy all move the response further, and none of them is
    visible to any code. So the strong term is a string the operator types,
    compared literally: unverifiable by construction, in the same family as
    ``DriverCeiling.basis`` and ``NoIsolation.basis``.

    The asymmetry that makes it safe: a token that changes when nothing moved
    costs a refused comparison, and a token that stays when something moved is
    a false verdict. Everything here is arranged so the first is the only
    failure mode reachable by accident.
    """

    def _prov(self, **kw):
        base = dict(
            device="iface",
            sample_rate_hz=44_100,
            gains_db=(0.0,),
            timestamp=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
            coupling=Coupling.ACOUSTIC,
            temperature_c=21.0,
            setup_token="driver seat, mic at headrest, doors shut",
        )
        base.update(kw)
        return Provenance(**base)

    def test_an_acoustic_measurement_without_a_token_compares_to_nothing(self):
        # The whole point. Temperature is recorded and inside tolerance, and
        # it is still not enough: nothing here says the microphone did not
        # move five centimetres, which at 3.5 kHz is a large fraction of a
        # wavelength.
        a = self._prov(setup_token=None)
        b = self._prov(setup_token=None)
        assert a.temperature_c == b.temperature_c
        assert not a.comparable_to(b)
        assert "no setup token" in a.why_incomparable(b)

    def test_two_matching_tokens_and_a_temperature_do_compare(self):
        assert self._prov().comparable_to(self._prov())

    def test_a_changed_token_beats_an_unchanged_thermometer(self):
        # Same temperature to the millikelvin, same gains, same interface.
        # The operator says the car is not set up the way it was, and that
        # ends the comparison.
        a = self._prov()
        b = self._prov(setup_token="passenger seat, mic at headrest, doors shut")
        assert a.temperature_c == b.temperature_c
        assert not a.comparable_to(b)
        assert "different setup token" in a.why_incomparable(b)

    def test_declaring_one_side_only_is_a_change_not_an_omission(self):
        # One measurement claims a setup and the other claims nothing. That
        # is not a match, and treating a missing declaration as a wildcard is
        # how the weaker of two claims wins.
        assert not self._prov().comparable_to(self._prov(setup_token=None))
        assert not self._prov(setup_token=None).comparable_to(self._prov())

    def test_electrical_needs_no_token(self):
        a = self._prov(coupling=Coupling.ELECTRICAL, setup_token=None)
        assert a.comparable_to(
            self._prov(coupling=Coupling.ELECTRICAL, setup_token=None)
        )

    def test_electrical_still_honours_a_token_that_was_declared(self):
        # A cable moved from OUT1 to OUT2 is a real change no other field
        # records. Not required on the bench; binding once claimed.
        a = self._prov(coupling=Coupling.ELECTRICAL, setup_token="OUT1 -> input 1")
        b = self._prov(coupling=Coupling.ELECTRICAL, setup_token="OUT2 -> input 1")
        assert not a.comparable_to(b)

    def test_a_blank_token_is_refused_at_construction(self):
        # An empty string would assert nothing while making two measurements
        # compare equal, which is strictly worse than omitting it.
        for blank in ("", "   ", "\t\n"):
            with pytest.raises(ValueError, match="blank"):
                self._prov(setup_token=blank)

    def test_surrounding_whitespace_does_not_split_a_setup(self):
        # Canonicalized at construction, then compared verbatim. Trimming the
        # ends discards nothing; anything more -- case folding, collapsing
        # internal runs -- would be leniency at comparison time, and leniency
        # is what lets two different setups match.
        assert self._prov(setup_token="  driver seat  ").setup_token == "driver seat"
        assert self._prov(setup_token=" x ").comparable_to(self._prov(setup_token="x"))
        assert not self._prov(setup_token="Driver Seat").comparable_to(
            self._prov(setup_token="driver seat")
        )

    def test_self_comparability_separates_structure_from_drift(self):
        # Comparing a provenance to itself cancels every pairwise term and
        # leaves only what is structurally required. That is what lets a run
        # discover "no verdict is reachable" before it fits or writes.
        assert self._prov().self_comparable()
        assert not self._prov(setup_token=None).self_comparable()
        assert not self._prov(temperature_c=None).self_comparable()
        assert self._prov(
            coupling=Coupling.ELECTRICAL, setup_token=None, temperature_c=None
        ).self_comparable()

    def test_the_reason_names_the_term_that_moved(self):
        # INDETERMINATE is otherwise an opaque verdict, and the run that
        # reports it has already spent several minutes and a device write.
        a = self._prov()
        assert "preamp gains" in a.why_incomparable(self._prov(gains_db=(6.0,)))
        assert "interface" in a.why_incomparable(self._prov(device="other"))
        assert "sample rate" in a.why_incomparable(self._prov(sample_rate_hz=48_000))
        assert "calibration" in a.why_incomparable(self._prov(cal_sha256="beef"))
        assert "coupling" in a.why_incomparable(
            self._prov(coupling=Coupling.ELECTRICAL)
        )
        assert "temperature" in a.why_incomparable(self._prov(temperature_c=40.0))
        assert a.why_incomparable(self._prov()) is None

    def test_the_raise_carries_the_reason(self):
        a = Measurement(impulse=np.zeros(8), provenance=self._prov())
        b = Measurement(
            impulse=np.zeros(8), provenance=self._prov(setup_token="somewhere else")
        )
        with pytest.raises(IncomparableProvenance, match="different setup token"):
            a.require_comparable(b)
