"""Deliberate faults injected into the stimulus, for bench known-answer work.

The point of this machinery is that a bench run has a *right answer*: plant a
filter, and a perfect correction is its exact inverse. An "it improved" run
cannot tell a good correction from a mediocre one -- this project has been
caught by that twice.

So these tests check two things. That the fault reaches the measurement (or
the whole exercise measures nothing), and that a faulted measurement can never
be mistaken for a real one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from tuner.measure.fault import FaultFilter
from tuner.measure.result import Coupling, IncomparableProvenance, Provenance

SR = 48_000
AXIS = np.geomspace(50.0, 15_000.0, 400)

#: A narrow suck-out at 1 kHz. Deep enough to be unmistakable, narrow enough
#: that a fitter has to actually find it rather than tilt the whole curve.
NOTCH = [(1000.0, -9.0, 3.0)]


def a_fault(bands=None, label="bench: planted 1 kHz notch") -> FaultFilter:
    return FaultFilter.from_peaking(bands or NOTCH, SR, label)


class TestTheFaultIsWhatItSaysItIs:
    def test_the_response_matches_the_band_that_was_planted(self):
        fault = a_fault()
        response = fault.response_db(AXIS)
        at_centre = response[int(np.argmin(np.abs(AXIS - 1000.0)))]
        assert at_centre == pytest.approx(-9.0, abs=0.2)
        # ...and it is narrow, not a tilt.
        assert response[0] == pytest.approx(0.0, abs=0.2)
        assert response[-1] == pytest.approx(0.0, abs=0.2)

    def test_filtering_a_signal_produces_that_response(self):
        """The known answer, end to end through the actual filtering.

        A response computed from coefficients and a response measured from
        filtered samples are two different code paths. If they disagree, a
        bench run would be scored against an answer the hardware never played.
        """
        fault = a_fault()
        # An impulse in, so the output *is* the impulse response and its FFT
        # *is* the transfer function -- exact, rather than a noisy estimate
        # from a random realisation.
        n = 1 << 15
        impulse = np.zeros(n)
        impulse[0] = 1.0
        measured = 20 * np.log10(
            np.abs(np.fft.rfft(fault.apply_to(impulse, SR))) + 1e-30
        )

        freqs = np.fft.rfftfreq(n, 1.0 / SR)
        band = (freqs > 100.0) & (freqs < 15_000.0)
        expected = fault.response_db(freqs[band])
        assert np.max(np.abs(measured[band] - expected)) < 0.01

    def test_a_multi_band_fault_is_the_sum_of_its_parts(self):
        fault = a_fault(
            [(200.0, 6.0, 1.0), (4000.0, -6.0, 2.0)], label="bench: tilt pair"
        )
        response = fault.response_db(AXIS)
        assert response[int(np.argmin(np.abs(AXIS - 200.0)))] == pytest.approx(
            6.0, abs=0.3
        )
        assert response[int(np.argmin(np.abs(AXIS - 4000.0)))] == pytest.approx(
            -6.0, abs=0.3
        )


class TestItRefusesToBeSilentlyWrong:
    def test_a_rate_mismatch_is_refused(self):
        """Biquad response warps near Nyquist.

        A chain designed at 48 kHz is a *different filter* at 44.1 kHz. Using
        it anyway would emit a fault other than the one the known answer
        describes, and the run would be scored against the wrong target with
        nothing to show it.
        """
        fault = a_fault()
        with pytest.raises(ValueError, match="warps near Nyquist"):
            fault.apply_to(np.zeros(128), 44_100)

    def test_an_unlabelled_fault_is_refused(self):
        with pytest.raises(ValueError, match="needs a label"):
            FaultFilter.from_peaking(NOTCH, SR, "   ")

    def test_a_malformed_section_array_is_refused(self):
        with pytest.raises(ValueError, match=r"\(n, 6\)"):
            FaultFilter(np.zeros((3, 5)), SR, "bad")

    def test_the_fingerprint_covers_the_coefficients_and_not_just_the_label(self):
        """A label that stayed put while the filter moved is the dangerous case.

        It is exactly the comparison that must not pass: two runs that both
        say "bench: planted notch" but planted different notches.
        """
        one = a_fault([(1000.0, -9.0, 3.0)], label="same words")
        two = a_fault([(1000.0, -3.0, 3.0)], label="same words")
        assert one.fingerprint() != two.fingerprint()
        assert a_fault().fingerprint() == a_fault().fingerprint()


class TestAFaultedMeasurementCannotPassAsReal:
    def _prov(self, fault: str | None) -> Provenance:
        return Provenance(
            device="bench",
            sample_rate_hz=SR,
            gains_db=(0.0,),
            timestamp=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
            coupling=Coupling.ACOUSTIC,
            temperature_c=21.0,
            setup_token="bench 2.1, mic on stand, unmoved",
            injected_fault=fault,
        )

    def test_faulted_and_clean_are_incomparable(self):
        clean = self._prov(None)
        faulted = self._prov(a_fault().fingerprint())
        assert not clean.comparable_to(faulted)
        assert "injected fault" in clean.why_incomparable(faulted)

    def test_two_different_faults_are_incomparable(self):
        one = self._prov(a_fault([(1000.0, -9.0, 3.0)], label="a").fingerprint())
        two = self._prov(a_fault([(1000.0, -3.0, 3.0)], label="b").fingerprint())
        assert not one.comparable_to(two)

    def test_the_same_fault_compares_fine(self):
        # The whole bench run carries one fault, so its own measurements must
        # remain comparable to each other -- otherwise no verdict is possible
        # and the exercise is pointless.
        same = a_fault().fingerprint()
        assert self._prov(same).comparable_to(self._prov(same))

    def test_two_clean_measurements_still_compare(self):
        # Vacuity check: the new term must not break the ordinary case.
        assert self._prov(None).comparable_to(self._prov(None))

    def test_the_raise_names_it(self):
        from tuner.measure.result import Measurement

        clean = Measurement(impulse=np.zeros(8), provenance=self._prov(None))
        faulted = Measurement(
            impulse=np.zeros(8), provenance=self._prov(a_fault().fingerprint())
        )
        with pytest.raises(IncomparableProvenance, match="injected fault"):
            clean.require_comparable(faulted)


class TestItReachesTheStimulusThroughCapture:
    def test_the_fault_lands_in_the_measurement_and_in_provenance(self, monkeypatch):
        """The wiring, checked where it actually matters.

        The deconvolution runs against the *unfiltered* sweep, so the fault
        has to show up as system response. If it did not, a bench run would
        quietly measure a system with no fault in it and report a flat
        correction as success.
        """
        from tuner.measure import capture as capture_mod

        played: list[np.ndarray] = []

        def fake_play_record(stimulus, *a, **kw):
            played.append(np.asarray(stimulus, dtype=np.float64))
            # Echo the stimulus straight back: the "system" is a wire, so
            # anything in the measured response came from the fault.
            n = int(round(kw.get("tail_s", 1.0) * SR)) + stimulus.size
            out = np.zeros((n, 1))
            out[: stimulus.size, 0] = stimulus
            return out

        monkeypatch.setattr(capture_mod, "play_record", fake_play_record)

        fault = a_fault()
        config = capture_mod.CaptureConfig(
            sample_rate_hz=SR,
            stop_hz=20_000.0,
            duration_s=0.5,
            tail_s=0.1,
            repeats=1,
            ramp=False,
            verify_quiet=False,
            input_channels=(0,),
            fault=fault,
        )
        session = capture_mod.SessionInfo(
            gains_db=(0.0,), temperature_c=21.0, setup_token="bench, unmoved"
        )
        result = capture_mod.capture_sweep(config, session)[0]

        assert result.provenance.injected_fault == fault.fingerprint()

        # Assert the notch is *there*, not that a gated measurement of it
        # matches the analytic curve point for point. Windowing the impulse
        # response necessarily shallows a narrow feature, and demanding an
        # exact match would only be pinning the gate length.
        freqs = np.geomspace(200.0, 8_000.0, 200)
        measured = result.magnitude_dbfs(freqs)
        at_notch = measured[int(np.argmin(np.abs(freqs - 1000.0)))]
        away = np.median(measured[(freqs < 400.0) | (freqs > 4000.0)])
        assert at_notch < away - 5.0, "the planted notch did not reach the measurement"
        assert float(np.argmin(measured)) == pytest.approx(
            np.argmin(np.abs(freqs - 1000.0)), abs=12
        )

    def test_a_capture_with_no_fault_records_none(self, monkeypatch):
        from tuner.measure import capture as capture_mod

        monkeypatch.setattr(
            capture_mod,
            "play_record",
            lambda stimulus, *a, **kw: np.zeros((stimulus.size + 100, 1)) + 1e-6,
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
        assert result.provenance.injected_fault is None
