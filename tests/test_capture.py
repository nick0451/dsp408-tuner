"""Tests for the end-to-end capture path.

Audio I/O is stubbed with a synthetic system of known delay and response, so
the whole orchestration -- safety ramp, deconvolution, alignment, provenance --
is exercised with no hardware attached and a known right answer.

The alignment tests are the important ones. They pin the convention that
``arrival_samples`` is simultaneously the propagation delay and the arrival's
index into ``impulse``; if those ever diverge, gating and time alignment
silently disagree about where the signal starts.
"""

from datetime import datetime

import numpy as np
import pytest
from scipy import signal

import tuner.measure.capture as capture_mod
from tuner.audio.io import LoopbackConfig
from tuner.measure.capture import (
    PROBE_DURATION_S,
    CaptureConfig,
    SessionInfo,
    capture_sweep,
)
from tuner.measure.qa import NoisyPath, SilentPath
from tuner.measure.result import NoTimingReference
from tuner.safety.limits import ChannelLimit, SafetyViolation

SR = 44_100
LOOPBACK_LATENCY = 5000  # interface round trip, must be removed by alignment
ACOUSTIC_DELAY = 300  # extra delay on the measurement channel


class FakeRig:
    """Stands in for play_record with a synthetic system of known behaviour."""

    def __init__(self, acoustic_delay=ACOUSTIC_DELAY, filt=None, clip=False):
        self.acoustic_delay = acoustic_delay
        self.filt = filt
        self.clip = clip
        self.calls = []

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
        self.calls.append(
            {"level_peak": float(np.max(np.abs(stimulus))), "n": stimulus.size}
        )
        padded = np.concatenate(
            [stimulus, np.zeros(int(round(tail_s * sample_rate_hz)))]
        )

        columns = list(input_channels)
        if loopback is not None and loopback.input_channel not in columns:
            columns.append(loopback.input_channel)

        n = padded.size + LOOPBACK_LATENCY + self.acoustic_delay + 16
        out = np.zeros((n, len(columns)))
        for i, ch in enumerate(columns):
            if loopback is not None and ch == loopback.input_channel:
                delay = LOOPBACK_LATENCY
                sig = padded
            else:
                delay = LOOPBACK_LATENCY + self.acoustic_delay
                sig = signal.lfilter(*self.filt, padded) if self.filt else padded
            out[delay : delay + sig.size, i] = sig
        if self.clip:
            out[:] = 1.0
        return out


@pytest.fixture
def rig(monkeypatch):
    r = FakeRig()
    monkeypatch.setattr(capture_mod, "play_record", r)
    return r


def cfg(**kw):
    base = dict(
        sample_rate_hz=SR,
        input_channels=(1,),
        loopback=LoopbackConfig(output_channel=1, input_channel=2),
        duration_s=1.0,
        tail_s=0.3,
        ir_length_s=0.5,
        level_dbfs=-30.0,
        ramp=False,
        repeats=1,
    )
    base.update(kw)
    return CaptureConfig(**base)


SESSION = SessionInfo(
    gains_db=(12.0,), temperature_c=21.5, setup_token="bench, mic on stand, unmoved"
)


class TestAlignment:
    def test_recovers_propagation_delay_with_loopback(self, rig):
        m = capture_sweep(cfg(), SESSION)[1]
        assert m.has_timing_reference
        assert m.delay_samples() == pytest.approx(ACOUSTIC_DELAY, abs=1)

    def test_interface_latency_is_removed(self, rig):
        # The rig adds 5000 samples of round-trip latency. If alignment used
        # the deconvolution's t_zero instead of the loopback arrival, the
        # reported delay would include it.
        m = capture_sweep(cfg(), SESSION)[1]
        assert m.delay_samples() < LOOPBACK_LATENCY / 2

    def test_arrival_index_equals_propagation_delay(self, rig):
        # The convention that must never drift: one number, both meanings.
        m = capture_sweep(cfg(), SESSION)[1]
        assert int(np.argmax(np.abs(m.impulse))) == m.arrival_samples

    @pytest.mark.parametrize("delay", [0, 120, 900])
    def test_various_delays(self, monkeypatch, delay):
        monkeypatch.setattr(capture_mod, "play_record", FakeRig(acoustic_delay=delay))
        m = capture_sweep(cfg(), SESSION)[1]
        assert m.delay_samples() == pytest.approx(delay, abs=1)


class TestWithoutLoopback:
    def test_delay_unavailable(self, monkeypatch):
        monkeypatch.setattr(capture_mod, "play_record", FakeRig())
        m = capture_sweep(cfg(loopback=None), SESSION)[1]
        assert not m.has_timing_reference
        with pytest.raises(NoTimingReference):
            m.delay_samples()

    def test_magnitude_still_available(self, monkeypatch):
        monkeypatch.setattr(capture_mod, "play_record", FakeRig())
        m = capture_sweep(cfg(loopback=None), SESSION)[1]
        mag = m.magnitude_dbfs(np.array([1000.0]))
        assert np.isfinite(mag).all()


class TestResponseRecovery:
    def test_recovers_a_known_filter(self, monkeypatch):
        b, a = signal.butter(2, 2000.0, btype="low", fs=SR)
        monkeypatch.setattr(capture_mod, "play_record", FakeRig(filt=(b, a)))
        m = capture_sweep(cfg(), SESSION)[1]

        freqs = np.geomspace(200.0, 8000.0, 100)
        measured = m.magnitude_dbfs(freqs)
        measured -= np.median(measured[freqs < 500])
        _, h = signal.freqz(b, a, worN=freqs, fs=SR)
        expected = 20 * np.log10(np.abs(h))
        expected -= np.median(expected[freqs < 500])

        assert np.max(np.abs(measured - expected)) < 1.0


class ColdStartRig(FakeRig):
    """A rig whose output carries nothing for the first ``dead`` samples.

    Models a Bluetooth A2DP sink: every capture opens a fresh stream, the link
    needs a few hundred milliseconds before any sound exists, and the capture
    window opens at playback start regardless. Measured on the bench
    2026-08-14 -- a cold 400 Hz tone came back 39 dB down, and a *partial*
    lead-in came back a repeatable 7 dB down, which is the worse failure
    because it reads as a real measurement.

    The dead region is applied to what is played, so a lead-in longer than it
    means only silence is lost.

    **What this double cannot express, stated rather than discovered.** It
    models a hard on/off, so a lead-in one sample short of ``dead`` still
    recovers to 0.12 dB here, while on hardware a 0.5 s lead-in returned a
    repeatable 7 dB error. The real link evidently starts gradually. So these
    tests pin the *mechanism* and the fix; the *sufficient duration* is a
    bench number and cannot be derived from this fake. Same lesson as the
    padding bug, one level out.
    """

    def __init__(self, dead: int, **kw):
        super().__init__(**kw)
        self.dead = dead

    def __call__(self, stimulus, *args, **kwargs):
        muted = np.asarray(stimulus, dtype=np.float64).copy()
        muted[: self.dead] = 0.0
        return super().__call__(muted, *args, **kwargs)


class TestLeadIn:
    """``lead_in_s`` exists so a slow-starting output does not eat the sweep."""

    DEAD = SR // 2  # half a second of stream that carries nothing

    def _response(self, monkeypatch, rig, **kw):
        monkeypatch.setattr(capture_mod, "play_record", rig)
        m = capture_sweep(cfg(**kw), SESSION)[1]
        freqs = np.geomspace(200.0, 8000.0, 100)
        db = m.magnitude_dbfs(freqs)
        return db - np.median(db)

    def test_lead_in_is_transparent_on_a_healthy_path(self, monkeypatch):
        """It must not change the answer where it is not needed."""
        without = self._response(monkeypatch, FakeRig())
        with_lead = self._response(monkeypatch, FakeRig(), lead_in_s=0.5)
        assert np.max(np.abs(with_lead - without)) < 0.05

    def test_cold_start_corrupts_the_measurement_without_a_lead_in(
        self, monkeypatch
    ):
        """The bug reproduces: same rig, no lead-in, materially wrong."""
        healthy = self._response(monkeypatch, FakeRig())
        cold = self._response(monkeypatch, ColdStartRig(self.DEAD))
        assert np.max(np.abs(cold - healthy)) > 3.0

    def test_a_sufficient_lead_in_recovers_it(self, monkeypatch):
        healthy = self._response(monkeypatch, FakeRig())
        fixed = self._response(
            monkeypatch,
            ColdStartRig(self.DEAD),
            lead_in_s=self.DEAD / SR + 0.1,
        )
        assert np.max(np.abs(fixed - healthy)) < 0.5

    def test_the_lead_in_reaches_the_safety_ramp_too(self, monkeypatch):
        """A cold path would otherwise fail the ramp's signal-present check.

        The ramp runs before the sweep and is therefore always the coldest
        thing in the run. Applying the lead-in only to the sweep would trade a
        silent wrong answer for a spurious abort.
        """
        rig = ColdStartRig(self.DEAD)
        monkeypatch.setattr(capture_mod, "play_record", rig)
        capture_sweep(
            cfg(ramp=True, level_dbfs=-20.0, lead_in_s=self.DEAD / SR + 0.1),
            SESSION,
        )
        probe = int(round(PROBE_DURATION_S * SR))
        assert all(c["n"] > probe for c in rig.calls)

    def test_negative_lead_in_is_refused(self):
        with pytest.raises(ValueError, match="lead_in_s"):
            cfg(lead_in_s=-0.1)


class TestSafetyRamp:
    def test_ramp_runs_probes_before_the_measurement(self, monkeypatch):
        r = FakeRig()
        monkeypatch.setattr(capture_mod, "play_record", r)
        capture_sweep(
            cfg(
                level_dbfs=-20.0,
                ramp=True,
                limit=ChannelLimit(),
                repeats=2,
                verify_quiet=False,
            ),
            SESSION,
        )

        probe_len = int(PROBE_DURATION_S * SR)
        probes, measurements = r.calls[:-2], r.calls[-2:]
        assert probes, "expected probe sweeps before the measurement"
        assert all(c["n"] == probe_len for c in probes)
        assert all(c["n"] == SR for c in measurements)  # full 1 s sweeps

    def test_ramp_starts_quiet_and_rises(self, monkeypatch):
        r = FakeRig()
        monkeypatch.setattr(capture_mod, "play_record", r)
        capture_sweep(cfg(level_dbfs=-20.0, ramp=True, verify_quiet=False), SESSION)
        peaks = [c["level_peak"] for c in r.calls]
        assert peaks[0] == pytest.approx(10 ** (-30 / 20), rel=1e-3)
        assert peaks[-1] > peaks[0]

    def test_no_ramp_means_one_call_per_repeat(self, rig):
        capture_sweep(cfg(ramp=False, repeats=1, verify_quiet=False), SESSION)
        assert len(rig.calls) == 1

    def test_clipping_aborts(self, monkeypatch):
        monkeypatch.setattr(capture_mod, "play_record", FakeRig(clip=True))
        with pytest.raises(SafetyViolation, match="clipping"):
            capture_sweep(cfg(verify_quiet=False), SESSION)


class TestProvenance:
    def test_records_operator_supplied_metadata(self, rig):
        m = capture_sweep(cfg(), SESSION, now=datetime(2026, 8, 6, 10, 0))[1]
        assert m.provenance.gains_db == (12.0,)
        assert m.provenance.temperature_c == 21.5
        assert m.provenance.sample_rate_hz == SR
        assert m.provenance.timestamp == datetime(2026, 8, 6, 10, 0)

    def test_gain_count_must_match_channel_count(self, rig):
        with pytest.raises(ValueError, match="gain for each"):
            capture_sweep(cfg(input_channels=(1, 3)), SessionInfo(gains_db=(0.0,)))

    def test_two_captures_are_comparable(self, rig):
        now = datetime(2026, 8, 6, 10, 0)
        a = capture_sweep(cfg(), SESSION, now=now)[1]
        b = capture_sweep(cfg(), SESSION, now=now)[1]
        a.require_comparable(b)


class TestConfigValidation:
    def test_rejects_stop_above_nyquist(self):
        with pytest.raises(ValueError, match="Nyquist"):
            CaptureConfig(sample_rate_hz=44_100, stop_hz=25_000.0)

    def test_rejects_empty_input_channels(self):
        with pytest.raises(ValueError, match="at least one"):
            CaptureConfig(input_channels=())


class TestMultiChannel:
    def test_returns_one_measurement_per_input(self, monkeypatch):
        monkeypatch.setattr(capture_mod, "play_record", FakeRig())
        out = capture_sweep(
            cfg(input_channels=(1, 3)), SessionInfo(gains_db=(10.0, 11.0))
        )
        assert set(out) == {1, 3}

    def test_loopback_channel_is_not_returned(self, rig):
        # It is consumed as the reference, not reported as a measurement.
        out = capture_sweep(cfg(), SESSION)
        assert 2 not in out


class NoisyRig(FakeRig):
    """FakeRig that also injects a floor, so silence does not come back silent."""

    def __init__(self, noise_dbfs=-30.0, **kw):
        super().__init__(**kw)
        self.noise_dbfs = noise_dbfs

    def __call__(self, stimulus, *a, **kw):
        out = super().__call__(stimulus, *a, **kw)
        rng = np.random.default_rng(0)
        return out + rng.normal(0.0, 10 ** (self.noise_dbfs / 20.0), out.shape)


class TestQuietGate:
    def test_runs_by_default_and_emits_silence(self, rig):
        capture_sweep(cfg(), SESSION)
        assert rig.calls[0]["level_peak"] == 0.0, (
            "the quiet check must be the first thing on the wire, and silent"
        )

    def test_can_be_disabled(self, rig):
        capture_sweep(cfg(verify_quiet=False), SESSION)
        assert rig.calls[0]["level_peak"] > 0.0

    def test_noisy_path_is_refused(self, monkeypatch):
        monkeypatch.setattr(capture_mod, "play_record", NoisyRig(noise_dbfs=-30.0))
        with pytest.raises(NoisyPath, match="idle noise floor"):
            capture_sweep(cfg(level_dbfs=-30.0), SESSION)

    def test_refusal_happens_before_any_stimulus(self, monkeypatch):
        r = NoisyRig(noise_dbfs=-30.0)
        monkeypatch.setattr(capture_mod, "play_record", r)
        with pytest.raises(NoisyPath):
            capture_sweep(cfg(level_dbfs=-30.0, ramp=True), SESSION)
        assert len(r.calls) == 1, "should abort on the silent probe"
        assert r.calls[0]["level_peak"] == 0.0

    def test_quiet_enough_floor_passes(self, monkeypatch):
        monkeypatch.setattr(capture_mod, "play_record", NoisyRig(noise_dbfs=-95.0))
        capture_sweep(cfg(level_dbfs=-30.0), SESSION)

    def test_min_snr_is_configurable(self, monkeypatch):
        monkeypatch.setattr(capture_mod, "play_record", NoisyRig(noise_dbfs=-95.0))
        with pytest.raises(NoisyPath):
            capture_sweep(cfg(level_dbfs=-30.0, min_snr_db=100.0), SESSION)


class TestDeviceProvenance:
    """Both directions must be recorded.

    MME reorders device indices when the Windows default output changes, so a
    hard-coded index can silently address the wrong output while the input stays
    correct. Recording only the input made that invisible in the record.
    """

    def test_records_both_input_and_output(self, monkeypatch):
        fake = {1: {"name": "Mic (Scarlett)"}, 3: {"name": "Speakers (Scarlett)"}}
        import sounddevice as sd

        monkeypatch.setattr(sd, "query_devices", lambda i: fake[i], raising=False)
        name = capture_mod._device_name((1, 3))
        assert "Mic (Scarlett)" in name and "Speakers (Scarlett)" in name

    def test_differing_output_breaks_comparability(self, monkeypatch):
        fake = {
            1: {"name": "Mic (Scarlett)"},
            3: {"name": "Speakers (Scarlett)"},
            7: {"name": "Speakers (Realtek)"},
        }
        import sounddevice as sd

        monkeypatch.setattr(sd, "query_devices", lambda i: fake[i], raising=False)
        good = capture_mod._device_name((1, 3))
        wrong_output = capture_mod._device_name((1, 7))
        assert good != wrong_output, (
            "a capture through a different output must not look comparable"
        )

    def test_never_raises(self):
        assert capture_mod._device_name((999, 999)).startswith("unknown(")


class SilentRig(FakeRig):
    """Returns only a noise floor -- the stimulus never arrives.

    Models the real failure: MME renumbered its devices, the sweep went to the
    PC speakers, and the correct input was captured hearing nothing.
    """

    def __init__(self, floor_dbfs=-70.0, constant_tone_dbfs=None, **kw):
        super().__init__(**kw)
        self.floor_dbfs = floor_dbfs
        self.constant_tone_dbfs = constant_tone_dbfs

    def __call__(self, stimulus, *a, **kw):
        out = super().__call__(stimulus, *a, **kw)
        rng = np.random.default_rng(1)
        y = rng.normal(0.0, 10 ** (self.floor_dbfs / 20.0), out.shape)
        if self.constant_tone_dbfs is not None:
            t = np.arange(out.shape[0]) / SR
            tone = 10 ** (self.constant_tone_dbfs / 20.0) * np.sin(
                2 * np.pi * 997.0 * t
            )
            y = y + tone[:, None]
        return y


class TestSignalPresent:
    def test_silent_path_is_refused(self, monkeypatch):
        monkeypatch.setattr(capture_mod, "play_record", SilentRig(floor_dbfs=-70.0))
        with pytest.raises(SilentPath, match="not reaching the input"):
            capture_sweep(
                cfg(level_dbfs=-20.0, ramp=True, limit=ChannelLimit()), SESSION
            )

    def test_constant_interferer_is_not_mistaken_for_signal(self, monkeypatch):
        """A steady tone lifts the level without responding to the stimulus.

        It clears the "loud enough" bar, so only the rising-with-level test
        catches it. Idle floor is measured with the same tone present, so the
        margin check passes and the response check must do the work.
        """
        rig = SilentRig(floor_dbfs=-90.0, constant_tone_dbfs=-20.0)
        monkeypatch.setattr(capture_mod, "play_record", rig)
        with pytest.raises(SilentPath):
            capture_sweep(
                cfg(
                    level_dbfs=-20.0, ramp=True, limit=ChannelLimit(), min_snr_db=-100.0
                ),
                SESSION,
            )

    def test_working_rig_passes(self, rig):
        capture_sweep(cfg(level_dbfs=-20.0, ramp=True, limit=ChannelLimit()), SESSION)

    def test_skipped_when_quiet_check_is_off(self, monkeypatch):
        """No idle baseline means no signal check -- opting out is explicit."""
        monkeypatch.setattr(capture_mod, "play_record", SilentRig(floor_dbfs=-70.0))
        capture_sweep(
            cfg(level_dbfs=-20.0, ramp=True, limit=ChannelLimit(), verify_quiet=False),
            SESSION,
        )

    def test_threshold_is_configurable(self, monkeypatch):
        """A real floor and a real signal, with the bar set above the margin."""
        monkeypatch.setattr(capture_mod, "play_record", NoisyRig(noise_dbfs=-70.0))
        # Signal lands ~50 dB above a -70 dBFS floor; demand 60 dB.
        with pytest.raises(SilentPath):
            capture_sweep(
                cfg(
                    level_dbfs=-20.0,
                    ramp=True,
                    limit=ChannelLimit(),
                    min_response_db=60.0,
                ),
                SESSION,
            )


class TestCombinePasses:
    """Median-of-N across passes -- the HF artifact, and its fix.

    This function had **no direct tests at all** until 2026-08-09, which is how
    a 6 dB error survived in it. It is the most load-bearing routine in the
    measurement engine: every magnitude figure the project reports goes through
    it, and everything downstream inherits whatever it gets wrong.

    The original implementation medianed the real and imaginary parts
    independently, which is not a complex median and is not rotation
    invariant. At the residual misalignment real captures actually see, it
    costs about 0.1-0.36 dB at 20 kHz -- **not** the multi-dB HF artifact,
    which remains unexplained -- but it degrades to 7.6 dB when alignment is
    poor, which is exactly when a measurement is already in trouble.
    """

    @staticmethod
    def _impulse_at(delay, length=4096, amplitude=1.0):
        x = np.zeros(length)
        x[delay] = amplitude
        return x

    @staticmethod
    def _spectrum(x, freqs_hz, sample_rate_hz=SR):
        from tuner.measure.metrics import frequency_response

        return frequency_response(x, sample_rate_hz, freqs_hz)

    def test_a_single_pass_is_returned_unchanged(self):
        x = self._impulse_at(100)
        assert capture_mod._combine_passes([x]) is x

    def test_identical_passes_combine_to_themselves(self):
        x = self._impulse_at(100)
        got = capture_mod._combine_passes([x.copy() for _ in range(3)])
        assert np.max(np.abs(got - x)) < 1e-9

    def test_phase_only_disagreement_does_not_attenuate(self):
        # Passes of an identical flat system differing only by a sub-sample
        # timing offset -- exactly what survives alignment. Magnitude must be
        # untouched at every frequency.
        base = self._impulse_at(2048)
        passes = [capture_mod._shift(base, off) for off in (-0.25, 0.0, 0.25)]
        combined = capture_mod._combine_passes(passes)

        freqs = np.array([1e3, 4e3, 8e3, 12e3, 16e3, 20e3])
        err_db = 20 * np.log10(np.abs(self._spectrum(combined, freqs)))
        assert np.max(np.abs(err_db)) < 0.05, dict(zip(freqs, err_db, strict=True))

    @pytest.mark.parametrize("residual", [0.05, 0.1, 0.2, 0.4])
    def test_it_survives_poor_alignment(self, residual):
        # The case that makes this worth fixing at all. Alignment normally
        # leaves 0.05-0.11 samples, but when it degrades the old combiner fell
        # apart: -1.5 dB at 0.2 samples, -7.6 dB at 0.4. Phase disagreement
        # alone must never move the magnitude.
        from scipy.fft import next_fast_len, rfft

        base = self._impulse_at(2048)
        n = next_fast_len(base.size)
        spectra = np.array(
            [rfft(capture_mod._shift(base, o), n) for o in (-residual, 0.0, residual)]
        )
        combined = np.median(np.abs(spectra), axis=0) * np.exp(
            1j * np.angle(spectra.sum(axis=0))
        )
        bins = np.fft.rfftfreq(n, 1 / SR)
        hf = np.abs(combined[(bins > 8000) & (bins < 21000)])
        assert np.max(np.abs(20 * np.log10(hf))) < 0.01

    def test_the_error_does_not_grow_with_frequency(self):
        # Pin the shape, not just the worst case: HF no worse than LF.
        base = self._impulse_at(2048)
        passes = [capture_mod._shift(base, off) for off in (-0.3, 0.05, 0.3)]
        combined = capture_mod._combine_passes(passes)

        def err(lo, hi):
            band = np.linspace(lo, hi, 40)
            return np.abs(20 * np.log10(np.abs(self._spectrum(combined, band))))

        assert err(8_000, 20_000).max() < err(200, 1_000).max() + 0.05

    def test_a_narrowband_dropout_is_still_rejected(self):
        # The median's whole purpose. The fix must not cost this.
        rng = np.random.default_rng(3)
        base = self._impulse_at(2048)
        good = [base + rng.normal(0, 1e-5, base.shape) for _ in range(2)]

        n = 8192
        spectrum = np.fft.rfft(base, n)
        bins = np.fft.rfftfreq(n, 1 / SR)
        spectrum[(bins > 3000) & (bins < 3400)] *= 0.1  # 20 dB dropout
        bad = np.fft.irfft(spectrum, n)[: base.size]

        combined = capture_mod._combine_passes([good[0], bad, good[1]])
        freqs = np.linspace(3050, 3350, 25)
        err_db = 20 * np.log10(np.abs(self._spectrum(combined, freqs)))
        assert np.max(np.abs(err_db)) < 0.5, "the dropout leaked through"

    def test_a_gross_timing_offset_is_aligned_out(self):
        # Round-trip latency drifts tens of samples between runs. Combining
        # unaligned passes comb-filters the result into something that looks
        # like a catastrophic system response.
        base = self._impulse_at(2048)
        passes = [base, self._impulse_at(2048 + 37), self._impulse_at(2048 - 21)]
        combined = capture_mod._combine_passes(passes)
        freqs = np.linspace(200, 18000, 60)
        err_db = 20 * np.log10(np.abs(self._spectrum(combined, freqs)))
        assert np.max(np.abs(err_db)) < 0.2

    def test_the_magnitude_stays_within_what_the_passes_spanned(self):
        # The coordinate-wise version could emit a magnitude below all of its
        # inputs -- 0.5 from three inputs of 1.0 -- which is the defect in one
        # line. A median cannot leave the range of its inputs.
        from scipy.fft import next_fast_len, rfft

        rng = np.random.default_rng(5)
        passes = [
            capture_mod._shift(self._impulse_at(2048), off) + rng.normal(0, 1e-4, 4096)
            for off in (-0.2, 0.0, 0.2)
        ]
        combined = capture_mod._combine_passes(passes)

        n = next_fast_len(4096)
        ref = passes[0]
        aligned = [ref] + [
            capture_mod._shift(p, -capture_mod._lag_samples(p, ref)) for p in passes[1:]
        ]
        spectra = np.abs(np.array([rfft(p, n) for p in aligned]))
        got = np.abs(rfft(combined, n))
        assert np.all(got >= spectra.min(axis=0) - 1e-9)
        assert np.all(got <= spectra.max(axis=0) + 1e-9)

    def test_an_even_number_of_passes_is_handled(self):
        base = self._impulse_at(2048)
        passes = [capture_mod._shift(base, off) for off in (-0.2, -0.05, 0.05, 0.2)]
        combined = capture_mod._combine_passes(passes)
        freqs = np.array([1e3, 10e3, 20e3])
        err_db = 20 * np.log10(np.abs(self._spectrum(combined, freqs)))
        assert np.max(np.abs(err_db)) < 0.05

    def test_the_old_coordinate_wise_median_fails_these(self):
        # Guards the guard. If a future change reverts to medianing real and
        # imaginary parts separately, this documents what that costs -- and
        # proves the tests above are actually sensitive to it.
        from scipy.fft import next_fast_len, rfft

        base = self._impulse_at(2048)
        # Deliberately unaligned, to isolate the combiner from the
        # alignment that normally masks most of this.
        passes = [capture_mod._shift(base, off) for off in (-0.4, 0.0, 0.4)]
        n = next_fast_len(base.size)
        spectra = np.array([rfft(p, n) for p in passes])
        old = np.median(spectra.real, axis=0) + 1j * np.median(spectra.imag, axis=0)

        bins = np.fft.rfftfreq(n, 1 / SR)
        hf = np.abs(old[bins > 15000])
        worst_db = 20 * np.log10(hf.min())
        assert worst_db < -1.0, (
            "the old implementation should attenuate at HF given this much "
            "phase disagreement; if it stops doing so, the tests above are no "
            "longer sensitive to the defect they exist for"
        )
