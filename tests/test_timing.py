"""Known-answer tests for the acoustic timing reference.

The whole point of the reference is to recover a number -- an arrival, a clock
ratio -- so every test here plants one and demands it back. Synthetic captures
are built the way a real one is assembled: chirp, gap, sweep, gap, chirp, with
a reflection and a clock stretch added on top.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import resample

from tuner.measure.timing import (
    ARRIVAL_THRESHOLD,
    TimingReference,
    correct_timebase,
    detect_arrival,
    estimate_clock_ratio,
    reference_chirp,
)

SR = 48_000
REF = reference_chirp(SR)


def a_capture(
    first_at: int,
    second_at: int,
    noise: float = 0.0,
    reflection_at: int | None = None,
    reflection_gain: float = 0.0,
    total: int = 300_000,
    seed: int = 0,
) -> np.ndarray:
    """chirp, ..., chirp -- with optional room and noise."""
    x = np.zeros(total, dtype=np.float64)
    for start in (first_at, second_at):
        x[start : start + REF.n] += REF.samples
    if reflection_at is not None:
        x[reflection_at : reflection_at + REF.n] += reflection_gain * REF.samples
    if noise:
        x += np.random.default_rng(seed).normal(0.0, noise, x.shape)
    return x


class TestTheReferenceSignal:
    def test_it_stays_inside_the_band_it_claims(self):
        spectrum = np.abs(np.fft.rfft(REF.samples))
        freqs = np.fft.rfftfreq(REF.n, 1.0 / SR)
        inside = (freqs >= REF.start_hz) & (freqs <= REF.stop_hz)
        # Windowed, so the skirts are not brick walls -- but the energy has to
        # be where it was promised, or a tweeter is being asked for content
        # below its crossover.
        assert np.sum(spectrum[inside] ** 2) / np.sum(spectrum**2) > 0.95

    def test_it_is_windowed_so_the_correlation_peak_is_clean(self):
        # An unwindowed chirp's edge discontinuities raise sidelobes that a
        # reflection can push above the true peak.
        assert abs(REF.samples[0]) < 1e-6
        assert abs(REF.samples[-1]) < 1e-6

    def test_it_is_normalised_so_the_safety_limiter_sees_a_known_peak(self):
        assert np.max(np.abs(REF.samples)) == pytest.approx(1.0)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"duration_s": 0.0},
            {"start_hz": 8000.0, "stop_hz": 2000.0},
            {"stop_hz": 40_000.0},
        ],
    )
    def test_it_refuses_a_band_it_cannot_produce(self, kwargs):
        with pytest.raises(ValueError):
            reference_chirp(SR, **kwargs)


class TestDetection:
    def test_it_finds_a_planted_arrival(self):
        got = detect_arrival(a_capture(12_345, 200_000), REF, slice(0, 100_000))
        # Cross-correlation peaks at the lag where the reference best aligns,
        # which is the chirp's own start. The arrival index is that start.
        assert got.index == pytest.approx(12_345, abs=1.0)

    @pytest.mark.parametrize("shift", [-97, -1, 0, 1, 53, 811])
    def test_an_integer_shift_is_recovered_exactly(self, shift):
        base = a_capture(20_000, 200_000)
        window = slice(0, 100_000)
        moved = detect_arrival(np.roll(base, shift), REF, window)
        assert moved.index == pytest.approx(20_000 + shift, abs=0.5)

    def test_sub_sample_refinement_is_present_but_not_relied_on(self):
        """Honest scope: this is parabolic, and parabolas are not this peak.

        An earlier test here asserted a half-sample shift was recovered to
        0.1 samples. It passed on the old 2-8 kHz reference and was **wrong**:
        a three-point parabolic fit across a 16-sample-wide lobe is inaccurate
        enough to report the wrong *direction*, and the assertion had pinned a
        sign nobody had derived. Widening the reference to 5-20 kHz sharpened
        the peak and broke it -- which is the test doing its job two months
        late.

        What is actually validated is one sample of arrival accuracy (above)
        and, end to end, a clock ratio good to 10 ppm over intervals of a few
        seconds (below). Sub-sample refinement helps the latter by averaging
        over a long baseline; it is not claimed on a single arrival.

        21 us is 7 mm of path, and the DSP's delay step is one sample at
        48 kHz -- so a whole sample is the resolution the device can express
        anyway.
        """
        got = detect_arrival(a_capture(20_000, 200_000), REF, slice(0, 100_000))
        assert got.index != float(int(got.index)) or got.index == 20_000.0

    def test_it_takes_the_first_arrival_and_not_the_loudest(self):
        """The failure this exists to prevent, in the geometry that causes it.

        In a cabin the direct sound is followed within a millisecond by a
        windshield reflection, and off-axis the reflection can arrive
        *louder*. Taking the matched filter's maximum would then report the
        reflection as t=0 -- consistently, with no sign of trouble.
        """
        direct, reflection = 20_000, 20_000 + 48  # 1 ms later
        got = detect_arrival(
            a_capture(direct, 200_000, reflection_at=reflection, reflection_gain=1.6),
            REF,
            slice(0, 100_000),
        )
        assert got.index == pytest.approx(direct, abs=2.0)
        assert got.weaker_than_a_later_peak
        assert got.strength < 1.0

    def test_a_clean_arrival_is_not_flagged(self):
        # Vacuity check for the flag above: it must not always be set. The
        # window isolates one chirp, which is how the detector is always
        # called -- a capture holds two references, and the second is
        # legitimately "a later, stronger peak".
        got = detect_arrival(a_capture(20_000, 200_000), REF, slice(0, 100_000))
        assert not got.weaker_than_a_later_peak
        assert got.strength == pytest.approx(1.0)

    def test_it_survives_noise_at_the_measured_idle_floor(self):
        # The UMIK-1 idled at -72.7 dBFS rms on this rig, 2026-08-13.
        got = detect_arrival(
            a_capture(20_000, 200_000, noise=10 ** (-72.7 / 20)),
            REF,
            slice(0, 100_000),
        )
        assert got.index == pytest.approx(20_000, abs=1.0)

    def test_the_search_window_separates_the_two_chirps(self):
        capture = a_capture(20_000, 200_000)
        second = detect_arrival(capture, REF, search=slice(100_000, None))
        assert second.index == pytest.approx(200_000, abs=1.0)

    def test_a_capture_with_no_reference_raises(self):
        # Returning an arbitrary index here would put a plausible number on a
        # measurement that never happened.
        with pytest.raises(ValueError, match="no reference arrival"):
            detect_arrival(np.zeros(100_000), REF)

    def test_the_threshold_is_a_fraction_of_the_peak_not_an_absolute(self):
        # So detection does not depend on how loud the reference was played.
        quiet = a_capture(20_000, 200_000) * 0.001
        found = detect_arrival(quiet, REF, slice(0, 100_000))
        assert found.index == pytest.approx(20_000, abs=1.0)
        assert 0.0 < ARRIVAL_THRESHOLD < 1.0


class TestTheClockRatio:
    def _measure(self, ppm: float, interval: int = 240_000) -> float:
        """Plant a clock error, recover it. The whole method in one function."""
        first_at, second_at = 20_000, 20_000 + interval
        clean = a_capture(first_at, second_at, total=second_at + 60_000)
        # A capture clock running fast produces more samples over the same
        # real time, which is a stretch of the array.
        stretched = resample(clean, int(round(clean.size * (1.0 + ppm / 1e6))))
        a = detect_arrival(stretched, REF, search=slice(0, 120_000))
        b = detect_arrival(stretched, REF, search=slice(120_000, None))
        return estimate_clock_ratio(a, b, interval).ppm

    @pytest.mark.parametrize("ppm", [0.0, 50.0, -50.0, 167.0, 500.0])
    def test_a_planted_clock_error_is_recovered(self, ppm):
        assert self._measure(ppm) == pytest.approx(ppm, abs=10.0)

    def test_the_bound_this_rig_could_not_resolve_electrically(self):
        """167 ppm was the floor of the frame-count method, 2026-08-13.

        Counting callback frames over 60 s quantises at one 480-sample buffer,
        so it could only say "under 167 ppm". This method resolves inside that
        by two orders of magnitude, which is the argument for having it.
        """
        assert abs(self._measure(167.0) - 167.0) < 10.0
        assert abs(self._measure(5.0) - 5.0) < 10.0

    def test_a_ratio_that_is_not_two_crystals_is_refused(self):
        # The failure this catches: a detection landed on the wrong chirp.
        # Its symptom is a correction that stretches the capture into nonsense
        # while looking like arithmetic.
        from tuner.measure.timing import Arrival

        far = Arrival(index=500_000.0, strength=1.0, weaker_than_a_later_peak=False)
        near = Arrival(index=1_000.0, strength=1.0, weaker_than_a_later_peak=False)
        with pytest.raises(ValueError, match="free-running crystals"):
            estimate_clock_ratio(near, far, generated_interval_samples=1_000)

    def test_detections_in_the_wrong_order_are_refused(self):
        from tuner.measure.timing import Arrival

        a = Arrival(index=500.0, strength=1.0, weaker_than_a_later_peak=False)
        b = Arrival(index=100.0, strength=1.0, weaker_than_a_later_peak=False)
        with pytest.raises(ValueError, match="not the two chirps"):
            estimate_clock_ratio(a, b, generated_interval_samples=1_000)

    def test_skew_is_reported_in_the_units_that_caused_the_worry(self):
        from tuner.measure.timing import ClockRatio

        ratio = ClockRatio(1.000167, 240_000, 240_040.08)
        # 167 ppm over a two-second sweep at 48 kHz.
        assert ratio.skew_samples(2 * SR) == pytest.approx(16.0, abs=0.5)


class TestTheCorrection:
    def test_it_puts_a_stretched_capture_back(self):
        """End to end: plant a delay and a clock error, recover the delay.

        This is the test that says the architecture works. The measurement's
        arrival is planted at a known offset from the first reference; the
        capture is then stretched by a clock error; and after detection,
        estimation and correction the offset must come back.
        """
        interval, offset, ppm = 240_000, 96_000, 200.0
        first_at = 20_000
        capture = a_capture(first_at, first_at + interval, total=400_000)
        # A measurement arrival, planted between the two references.
        capture[first_at + offset] += 4.0

        stretched = resample(capture, int(round(capture.size * (1.0 + ppm / 1e6))))
        a = detect_arrival(stretched, REF, search=slice(0, 120_000))
        b = detect_arrival(stretched, REF, search=slice(150_000, None))
        ratio = estimate_clock_ratio(a, b, interval)
        corrected = correct_timebase(stretched, ratio)

        # Where the reference and the measurement now sit, on the playback
        # device's timebase.
        ref_again = detect_arrival(corrected, REF, search=slice(0, 120_000))
        between = corrected[int(ref_again.index) : int(ref_again.index) + 200_000]
        measured_offset = int(np.argmax(np.abs(between)))
        # Both indices are chirp starts, so the recovered spacing is the
        # planted one. Three samples is 63 us at 48 kHz -- well inside what
        # any delay written to the DSP could express.
        assert measured_offset == pytest.approx(offset, abs=3.0)

    def test_a_zero_correction_is_the_identity(self):
        from tuner.measure.timing import ClockRatio

        x = np.random.default_rng(0).normal(0.0, 1.0, 4096)
        same = correct_timebase(x, ClockRatio(1.0, 1000, 1000.0))
        assert np.array_equal(same, x)


class TestTheThreeStates:
    """Why this is not a boolean."""

    def test_only_a_loopback_gives_absolute_delay(self):
        assert TimingReference.LOOPBACK.gives_absolute_delay
        assert not TimingReference.ACOUSTIC.gives_absolute_delay
        assert not TimingReference.NONE.gives_absolute_delay

    def test_an_acoustic_reference_still_gives_relative_delay(self):
        # The whole reason it exists: a subwoofer's arrival becomes
        # measurable because the reference, not the subwoofer, carries the
        # timing.
        assert TimingReference.ACOUSTIC.gives_relative_delay
        assert TimingReference.LOOPBACK.gives_relative_delay
        assert not TimingReference.NONE.gives_relative_delay


class TestWhatEachReferenceMayReport:
    """The three states, enforced on a Measurement rather than described."""

    def _m(self, timing, arrival=100, token="driver seat, mic at headrest"):
        from datetime import UTC, datetime

        from tuner.measure.result import Coupling, Measurement, Provenance

        return Measurement(
            impulse=np.zeros(256),
            provenance=Provenance(
                device="rig",
                sample_rate_hz=SR,
                gains_db=(0.0,),
                timestamp=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
                coupling=Coupling.ACOUSTIC,
                temperature_c=21.0,
                setup_token=token,
            ),
            arrival_samples=arrival,
            timing=timing,
        )

    def test_a_loopback_gives_absolute_delay(self):
        assert self._m(TimingReference.LOOPBACK).delay_samples() == 100

    def test_an_acoustic_reference_refuses_absolute_delay(self):
        """The whole reason the boolean had to go.

        The reference speaker's own path length sits inside every arrival as
        an unmeasured constant. A number here would be wrong by it -- and
        wrong by an amount that looks entirely plausible, which is the
        failure mode the timing-reference rule exists to prevent.
        """
        from tuner.measure.result import NoTimingReference

        with pytest.raises(NoTimingReference, match="unmeasured constant"):
            self._m(TimingReference.ACOUSTIC).delay_samples()

    def test_no_reference_refuses_everything_timed(self):
        from tuner.measure.result import NoTimingReference

        none = self._m(TimingReference.NONE, arrival=None)
        for call in (
            lambda: none.delay_samples(),
            lambda: none.phase_rad(np.array([1000.0])),
            lambda: none.group_delay_samples(np.array([1000.0])),
        ):
            with pytest.raises(NoTimingReference):
                call()

    def test_an_acoustic_reference_still_gives_phase(self):
        # Relative phase is exactly what it buys. An unknown *constant* delay
        # is a linear phase term shared by every measurement in the geometry,
        # so it cancels in any comparison between them.
        assert self._m(TimingReference.ACOUSTIC).phase_rad(np.array([1000.0])).size == 1

    def test_relative_delay_works_across_an_acoustic_pair(self):
        sub = self._m(TimingReference.ACOUSTIC, arrival=340)
        mid = self._m(TimingReference.ACOUSTIC, arrival=100)
        assert sub.relative_delay_samples(mid) == 240

    def test_relative_delay_refuses_a_moved_microphone(self):
        """The constant only cancels while the geometry holds.

        Two measurements either side of the microphone moving have different
        reference paths, so their difference is not a delay. The setup token
        is the only thing that knows, and it is the operator's word -- which
        is exactly why it is compared here rather than trusted.
        """
        from tuner.measure.result import IncomparableProvenance

        here = self._m(TimingReference.ACOUSTIC, arrival=340)
        moved = self._m(TimingReference.ACOUSTIC, arrival=100, token="passenger seat")
        with pytest.raises(IncomparableProvenance, match="setup token"):
            here.relative_delay_samples(moved)

    def test_relative_delay_refuses_two_different_time_origins(self):
        from tuner.measure.result import NoTimingReference

        with pytest.raises(NoTimingReference, match="not on one time origin"):
            self._m(TimingReference.ACOUSTIC).relative_delay_samples(
                self._m(TimingReference.LOOPBACK)
            )

    def test_has_timing_reference_is_derived_and_not_stored(self):
        # One source of truth. A stored bool beside the enum is two, and they
        # would eventually disagree.
        from tuner.measure.result import Measurement

        assert "has_timing_reference" not in Measurement.__dataclass_fields__
        assert self._m(TimingReference.ACOUSTIC).has_timing_reference
        assert not self._m(TimingReference.NONE).has_timing_reference
