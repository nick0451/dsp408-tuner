"""End-to-end capture: stimulus out, :class:`Measurement` back.

Ties together sweep generation, the safety ramp, audio I/O, deconvolution and
provenance assembly. This is the only sanctioned way to produce a
:class:`~tuner.measure.result.Measurement` from hardware -- ad-hoc scripts that
wire the pieces together by hand tend to skip the ramp or forget provenance,
and both failures are silent.

Alignment follows the convention documented on :class:`Measurement`: the
returned impulse has sample 0 at the timing-reference instant, so the arrival
index and the propagation delay are the same number by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.fft import irfft, next_fast_len, rfft, rfftfreq

from ..audio.io import LoopbackConfig, SplitDevices, play_record
from ..cal.calfile import file_sha256
from ..safety.limits import (
    START_LEVEL_DBFS,
    ChannelLimit,
    apply,
    assert_capture_sane,
    ramp_levels_dbfs,
)
from .deconv import deconvolve, peak_index
from .fault import FaultFilter
from .qa import (
    DEFAULT_MIN_RESPONSE_DB,
    DEFAULT_MIN_SNR_DB,
    IdleNoiseResult,
    SilentPath,
    analyze_idle_noise,
    require_quiet_path,
    require_signal_response,
    rms_dbfs,
)
from .result import Coupling, Measurement, PassSpread, Provenance
from .sweep import Sweep, log_sweep
from .timing import TimingReference

#: Length of the cheap probe sweeps used during the safety ramp. Long enough
#: to excite the system and catch a misroute, short enough that ramping does
#: not dominate measurement time.
PROBE_DURATION_S = 0.25


@dataclass(frozen=True)
class SessionInfo:
    """Metadata the software cannot discover and the operator must supply.

    Preamp gain settings are not readable over USB on most interfaces, and
    ambient temperature needs a thermometer. Both are asked for explicitly
    rather than defaulted to something plausible -- see the provenance rule in
    CLAUDE.md.

    ``coupling`` says whether the signal reached the input through air or
    through a cable, and it decides whether the environmental terms
    participate in comparability at all. It defaults to acoustic, the stricter
    reading; a bench measurement that never declares itself gets treated as
    though a room were involved. **Declaring ELECTRICAL is a claim about the
    wiring**, in the same family as ``DriverCeiling.basis`` -- no code can
    check it.

    ``setup_token`` is the operator's verbatim claim about the physical
    configuration, and it is **required for an acoustic session**. Write down
    what would invalidate a comparison if it changed -- where the microphone
    is, which seat, doors and windows, HVAC, who is in the car -- and change
    the token whenever any of it does. It is compared literally, so the safe
    failure is a false "incomparable", never a false "comparable".
    """

    gains_db: tuple[float, ...]
    temperature_c: float | None = None
    cal_file: Path | None = None
    coupling: Coupling = Coupling.ACOUSTIC
    setup_token: str | None = None
    notes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CaptureConfig:
    """How to run one capture.

    Defaults target the interim Scarlett rig: 44.1 kHz because that is its
    native rate, and a sweep wider than the band of interest because response
    degrades within an octave of the sweep endpoints.
    """

    sample_rate_hz: int = 44_100
    device: str | int | tuple[int, int] | SplitDevices | None = None
    output_channel: int = 0
    input_channels: tuple[int, ...] = (1,)
    loopback: LoopbackConfig | None = None

    start_hz: float = 10.0
    stop_hz: float = 20_000.0
    duration_s: float = 2.0
    tail_s: float = 1.0

    level_dbfs: float = START_LEVEL_DBFS
    limit: ChannelLimit = ChannelLimit()
    ramp: bool = True

    #: Length of impulse response retained, from the timing reference onward.
    ir_length_s: float = 1.0

    #: Sweeps to run and combine by median. Three is the minimum that can
    #: reject an outlier; one cannot detect a dropout at all.
    repeats: int = 3

    #: A deliberate fault filtered into the stimulus, for bench known-answer
    #: work. See :mod:`tuner.measure.fault`. Applied **before** the safety
    #: limiter, so it shapes the stimulus and cannot raise its level, and the
    #: deconvolution still runs against the unfiltered sweep -- so the fault
    #: reads as part of the system under test, which is the point.
    #:
    #: Its fingerprint lands in provenance and makes the capture incomparable
    #: to a clean one. A corrupted measurement must not be able to masquerade
    #: as a real one.
    fault: FaultFilter | None = None

    #: Check the idle noise floor before emitting anything. On by default: the
    #: interference this catches does not look like interference afterwards,
    #: it looks like a smooth and slightly wrong frequency response.
    verify_quiet: bool = True

    #: Headroom the idle floor must leave below ``level_dbfs``.
    min_snr_db: float = DEFAULT_MIN_SNR_DB

    #: How far the loudest ramp probe must lift the input above the idle
    #: floor before the chain counts as actually carrying our stimulus.
    min_response_db: float = DEFAULT_MIN_RESPONSE_DB

    #: Silence prepended to every emitted buffer, and sliced back off the
    #: recording before anything looks at it.
    #:
    #: **For output paths that take time to start carrying audio.** A
    #: Bluetooth A2DP sink is the case this exists for: each capture opens a
    #: fresh stream, and the link needs a few hundred milliseconds before any
    #: sound exists, while the capture window opens at *playback start*.
    #:
    #: Measured on the bench 2026-08-14, DSP-408 A2DP sink, 400 Hz tone after
    #: a 15 s idle, two cold trials each:
    #:
    #: ===========  ====================  =========================
    #: ``lead_in``  captured              verdict
    #: ===========  ====================  =========================
    #: 0.0 s        -88.8, -87.6 dB       dead, reproducibly
    #: 0.5 s        -56.5, -56.2 dB       **consistent and 7 dB low**
    #: 1.0 s        -49.9, -49.4 dB       correct
    #: 1.5 s        -49.4, -49.6 dB       correct
    #: ===========  ====================  =========================
    #:
    #: The 0.5 s row is the dangerous one and the reason this is a measured
    #: constant rather than a guess: a partial lead-in returns a *repeatable*
    #: wrong level, which reads as a real measurement. Zero at least
    #: deconvolves to obvious nonsense.
    #:
    #: Zero by default, because a wired path needs none and prepending silence
    #: to every capture would lengthen every measurement in the project for a
    #: problem it does not have.
    lead_in_s: float = 0.0

    def __post_init__(self) -> None:
        if self.repeats < 1:
            raise ValueError("repeats must be at least 1")
        if self.lead_in_s < 0:
            raise ValueError("lead_in_s cannot be negative")
        if self.stop_hz >= self.sample_rate_hz / 2:
            raise ValueError(
                f"stop_hz {self.stop_hz} is at or above Nyquist "
                f"({self.sample_rate_hz / 2}); lower it or raise the rate"
            )
        if not self.input_channels:
            raise ValueError("need at least one input channel")


def _device_name(device: str | int | tuple[int, int] | SplitDevices | None) -> str:
    """Human-readable device identity for provenance -- both directions.

    Recording only the input is not enough. MME reorders its device indices
    when the Windows default output changes (it lists the default first), so a
    hard-coded index can silently start addressing a different device. Measured
    on the bench: the interface moved from output index 3 to 7 when the default
    was switched, and the sweep went to the PC's speakers while the correct
    input was still captured -- yielding a plausible-looking noise measurement
    whose provenance named the right interface, because only the input was
    recorded.

    Prefer selecting devices by name over index for the same reason. This
    string also feeds :meth:`Provenance.comparable_to`, so a measurement taken
    through a different output will no longer compare equal to one that was not.
    """
    try:
        import sounddevice as sd

        if isinstance(device, SplitDevices):
            # Two devices, two clocks. Both names matter here for the same
            # reason a tuple's do -- and more so, because a split capture is
            # the configuration most likely to be re-cabled between sessions.
            in_dev, out_dev = device.input, device.output
        elif isinstance(device, tuple):
            in_dev, out_dev = device
        else:
            in_dev = out_dev = device
        if in_dev is None:
            in_dev = sd.default.device[0]
        if out_dev is None:
            out_dev = sd.default.device[1]
        in_name = str(sd.query_devices(in_dev)["name"])
        out_name = str(sd.query_devices(out_dev)["name"])
        return f"in={in_name} | out={out_name}"
    except Exception:  # pragma: no cover - provenance must never block a capture
        return f"unknown({device!r})"


#: Length of the silent capture taken before a measurement. Long enough to
#: resolve mains harmonics, short enough not to be felt.
QUIET_PROBE_DURATION_S = 1.0


def _with_fault(config: CaptureConfig, samples: np.ndarray) -> np.ndarray:
    """Filter the stimulus through the configured fault, if there is one."""
    if config.fault is None:
        return samples
    return config.fault.apply_to(samples, config.sample_rate_hz)


def _verify_quiet(config: CaptureConfig) -> dict[int, IdleNoiseResult]:
    """Refuse to measure through a path that is not quiet at rest.

    The companion precondition to the level-linearity check, and for the same
    reason -- both catch failures that produce a plausible curve rather than an
    error. Two cases from the bench: a USB control cable sharing the
    interface's ground raised the floor 43 dB, and a video playing on the
    host's default output (the same interface) shifted a measured crossover
    corner by 6% while leaving the curve looking entirely reasonable.

    Runs before the safety ramp, so a rig this broken costs no stimulus at all.
    """
    n = int(round(QUIET_PROBE_DURATION_S * config.sample_rate_hz))
    captured = play_record(
        np.zeros(n, dtype=np.float64),
        output_channel=config.output_channel,
        input_channels=list(config.input_channels),
        sample_rate_hz=config.sample_rate_hz,
        device=config.device,
        loopback=config.loopback,
        tail_s=0.0,
        max_peak_dbfs=config.limit.ceiling_dbfs,
    )
    idle = {}
    for column, channel in enumerate(config.input_channels):
        result = analyze_idle_noise(captured[:, column], config.sample_rate_hz)
        require_quiet_path(result, config.level_dbfs, config.min_snr_db)
        idle[channel] = result
    return idle


def _play_with_lead_in(config: CaptureConfig, stimulus: np.ndarray, **kwargs):
    """``play_record`` with ``config.lead_in_s`` of silence in front, removed
    again from the recording.

    Prepending to the *played* buffer and slicing the same count off the
    *recording* leaves every caller seeing exactly what it would have seen on
    a path that needed no lead-in -- same length, same sample 0, same arrival
    index. That matters because ``_single_pass`` takes its time origin from
    ``sweep.t_zero_index`` when there is no loopback, and a lead-in that
    shifted the recording would move the arrival without moving that index.

    Slicing rather than compensating the origin is deliberate: it keeps the
    correction in one place instead of spreading an offset through the
    deconvolution, the alignment and the ramp's RMS windows.
    """
    lead = int(round(config.lead_in_s * config.sample_rate_hz))
    if lead <= 0:
        return play_record(stimulus, **kwargs)

    padded = np.concatenate([np.zeros(lead, dtype=stimulus.dtype), stimulus])
    recorded = play_record(padded, **kwargs)
    if recorded.shape[0] <= lead:
        raise SilentPath(
            f"capture is {recorded.shape[0]} frames but the lead-in alone is "
            f"{lead}; nothing of the stimulus was recorded"
        )
    return recorded[lead:]


def _run_safety_ramp(
    config: CaptureConfig,
    sweep_gen,
    idle: dict[int, IdleNoiseResult] | None = None,
) -> None:
    """Step up to the target level, aborting if anything looks wrong.

    Rule 2 exists so that a misrouted channel, a wrong gain setting or an
    unexpectedly efficient driver is caught while it is still quiet. Full-length
    sweeps at every step would make that prohibitively slow, so each rung uses a
    short probe -- the point is to verify the signal chain, not to measure it.

    The rungs double as the signal-present check when ``idle`` is supplied: the
    ramp already plays known, increasing levels, so confirming that the input
    follows them costs nothing beyond the arithmetic.
    """
    levels = ramp_levels_dbfs(config.level_dbfs)
    probe_levels: list[float] = []
    captured_rms: dict[int, list[float]] = {ch: [] for ch in config.input_channels}
    for level in levels[:-1]:
        probe = sweep_gen(PROBE_DURATION_S)
        # The ramp carries the fault too. Its job is to catch a chain that is
        # not what the code thinks it is, and a ramp probing an unfaulted
        # signal would be verifying a chain the measurement never uses.
        stimulus = apply(_with_fault(config, probe.samples), level, config.limit)
        captured = _play_with_lead_in(
            config,
            stimulus,
            output_channel=config.output_channel,
            input_channels=list(config.input_channels),
            sample_rate_hz=config.sample_rate_hz,
            device=config.device,
            loopback=config.loopback,
            tail_s=0.2,
            max_peak_dbfs=config.limit.ceiling_dbfs,
        )
        probe_levels.append(level)
        for column, channel in enumerate(config.input_channels):
            assert_capture_sane(captured[:, column], channel=channel)
            captured_rms[channel].append(rms_dbfs(captured[:, column]))

    if idle is not None:
        for channel in config.input_channels:
            require_signal_response(
                idle[channel],
                probe_levels,
                captured_rms[channel],
                config.min_response_db,
                channel=channel,
            )


def _lag_samples(signal_: np.ndarray, reference: np.ndarray) -> float:
    """Sub-sample lag of ``signal_`` relative to ``reference``.

    Cross-correlation for the integer part, parabolic interpolation of the
    correlation peak for the fraction. The fraction matters: half a sample at
    15 kHz is 60 degrees of phase, which is enough to comb-filter an average.
    """
    n = next_fast_len(signal_.size + reference.size)
    correlation = irfft(rfft(signal_, n) * np.conj(rfft(reference, n)), n)

    k = int(np.argmax(correlation))
    before, at, after = (
        correlation[(k - 1) % n],
        correlation[k],
        correlation[(k + 1) % n],
    )
    curvature = before - 2.0 * at + after
    fraction = 0.5 * (before - after) / curvature if curvature != 0 else 0.0

    lag = k + fraction
    return lag - n if lag > n / 2 else lag


def _shift(signal_: np.ndarray, lag: float) -> np.ndarray:
    """Delay ``signal_`` by ``lag`` samples, fractional lags included."""
    if lag == 0.0:
        return signal_
    n = next_fast_len(signal_.size)
    spectrum = rfft(signal_, n)
    spectrum *= np.exp(-2j * np.pi * rfftfreq(n) * lag)
    return irfft(spectrum, n)[: signal_.size]


def _combine_passes(stack: list[np.ndarray]) -> np.ndarray:
    """Align passes to sub-sample precision, then median them per frequency bin.

    Two decisions here, both learned the hard way on real hardware:

    **Alignment is mandatory, not a refinement.** Round-trip latency drifts tens
    of samples between consecutive runs (see docs/hardware.md). Combining
    unaligned passes comb-filters the result, and the damage looks like a
    catastrophic system response rather than an averaging bug -- 24 dB of span
    on a loopback that is flat to a third of a dB.

    **The median must be taken per frequency bin, not per time sample.** A
    dropout is narrowband, so in the time domain it is low-level ringing spread
    across thousands of samples and is an outlier at none of them; a sample-wise
    median hardly rejects it. In the frequency domain it is confined to a few
    bins, where a median removes it cleanly. Measured difference on the interim
    rig: 1.71 dB span sample-wise versus well under 1 dB per-bin.

    Median rather than mean throughout, because a mean is dragged by the very
    outlier it is meant to reject.

    **Magnitude and phase are combined separately: median of the magnitudes,
    phase of the coherent sum.** Changed 2026-08-09; the reasoning matters
    because the obvious alternatives are both wrong.

    The previous implementation medianed the real and imaginary parts
    independently. That is not a complex median and it is not rotation
    invariant -- three unit phasors 120 degrees apart give
    ``median(re) + i*median(im) = -0.5``, a magnitude of 0.5 from three inputs
    that all had magnitude 1.0. Since a residual timing offset is a phase ramp,
    the damage grows with frequency, and measured against pure phase
    disagreement it costs 0.36 dB at 20 kHz for 0.1 samples of residual and
    **7.6 dB for 0.4 samples**.

    In practice the alignment above keeps the residual to roughly 0.05-0.11
    samples (the parabolic peak estimator is biased: a true 0.25-sample offset
    estimates as 0.14), so the realised error was only about 0.1-0.36 dB at
    20 kHz. **That is not large enough to be the HF artifact, which remains
    unexplained** -- but it is a latent hazard that becomes severe exactly when
    alignment degrades, which is when the measurement is already in trouble.

    Two candidate fixes were measured against each other. Selecting the whole
    complex value of the median-magnitude pass keeps every bin a real
    measurement, but when the passes have near-identical magnitudes the choice
    is arbitrary per bin, the phase comes out jagged, and the recombined
    impulse smears -- 0.27 dB of ripple on the full pipeline, worse than what it
    replaced. Taking the median magnitude with the phase of the coherent sum
    has neither problem: 0.001 dB on the same test, and exactly 0.000 dB
    against pure phase disagreement at every residual tried.

    Dropout rejection, the whole point of a median, is preserved: a dropout is
    a low-magnitude outlier so it is never the median, and it contributes
    little to the sum whose phase is used.
    """
    if len(stack) == 1:
        return stack[0]

    spectra, length, n = _aligned_spectra(stack)
    magnitude = np.median(np.abs(spectra), axis=0)
    phase = np.angle(spectra.sum(axis=0))
    return irfft(magnitude * np.exp(1j * phase), n)[:length]


def _aligned_spectra(stack: list[np.ndarray]) -> tuple[np.ndarray, int, int]:
    """Align passes to each other and transform them. Shared, not duplicated.

    Both the combiner and the spread need exactly this, and computing the
    alignment twice would risk the two disagreeing about which passes were
    compared -- which would make the spread a description of a combination
    that never happened.
    """
    length = stack[0].size
    reference = stack[0]
    aligned = [reference] + [_shift(p, -_lag_samples(p, reference)) for p in stack[1:]]
    n = next_fast_len(length)
    return np.array([rfft(p, n) for p in aligned]), length, n


def _pass_spread(stack: list[np.ndarray], sample_rate_hz: int) -> PassSpread | None:
    """Per-bin disagreement between repeats. None when there is only one.

    Peak-to-peak rather than a standard deviation, for the same reason the
    session repeatability floor uses a spread: at three repeats the spread is
    the honest bound on how far one measurement might be off, and a deviation
    from three samples is a statistic pretending to be one.
    """
    if len(stack) < 2:
        return None
    spectra, _length, n = _aligned_spectra(stack)
    magnitudes = np.abs(spectra)
    high = 20.0 * np.log10(np.max(magnitudes, axis=0) + 1e-30)
    low = 20.0 * np.log10(np.min(magnitudes, axis=0) + 1e-30)
    return PassSpread(
        freqs_hz=rfftfreq(n, 1.0 / sample_rate_hz),
        spread_db=high - low,
        n_passes=len(stack),
    )


def _single_pass(
    config: CaptureConfig,
    sweep: Sweep,
    stimulus: np.ndarray,
) -> tuple[dict[int, np.ndarray], bool]:
    """One sweep. Returns aligned impulses per channel, and whether a
    timing reference was available.

    Each pass is aligned independently, because round-trip latency is not
    repeatable between runs -- see docs/hardware.md. Aligning per pass is what
    makes averaging across passes meaningful at all.
    """
    recorded = _play_with_lead_in(
        config,
        stimulus,
        output_channel=config.output_channel,
        input_channels=list(config.input_channels),
        sample_rate_hz=config.sample_rate_hz,
        device=config.device,
        loopback=config.loopback,
        tail_s=config.tail_s,
        max_peak_dbfs=config.limit.ceiling_dbfs,
    )

    channels = list(config.input_channels)
    if config.loopback is not None and config.loopback.input_channel not in channels:
        channels.append(config.loopback.input_channel)

    for column, channel in enumerate(channels):
        assert_capture_sane(recorded[:, column], channel=channel)

    impulses = {
        channel: deconvolve(recorded[:, column], sweep.inverse)
        for column, channel in enumerate(channels)
    }

    # Sample 0 of the stored impulse is the timing-reference instant. With a
    # loopback that is the reference arrival, which removes interface latency
    # and makes the arrival index equal the propagation delay.
    if config.loopback is not None:
        origin = peak_index(impulses[config.loopback.input_channel])
        has_reference = True
    else:
        origin = sweep.t_zero_index
        has_reference = False

    length = int(round(config.ir_length_s * config.sample_rate_hz))
    aligned = {}
    for channel in config.input_channels:
        segment = impulses[channel][origin : origin + length]
        padded = np.zeros(length, dtype=np.float64)
        padded[: segment.size] = segment
        aligned[channel] = padded
    return aligned, has_reference


def capture_sweep(
    config: CaptureConfig,
    session: SessionInfo,
    now: datetime | None = None,
) -> dict[int, Measurement]:
    """Run a capture and return a :class:`Measurement` per input channel.

    Keyed by input channel number. The loopback channel, if any, is consumed
    as the timing reference and is not returned as a measurement.

    ``config.repeats`` sweeps are run, aligned to each other to sub-sample
    precision, and combined **per frequency bin** -- median of the magnitudes,
    phase of the coherent sum. See :func:`_combine_passes` for why each half is
    what it is; this docstring said "sample-wise median" until 2026-08-09,
    which described neither the code before that date nor after it.

    This is not optional politeness: MME drops samples, producing narrow-band
    artifacts of several dB at frequencies that move between runs. A single
    sweep cannot distinguish such an artifact from a real response feature; the
    median across repeats removes them because they do not recur in the same
    place. Measured on the interim rig, three runs spanning 2.7, 1.2 and 4.0 dB
    individually gave a median curve spanning 0.65 dB.

    Median rather than mean, because a mean is dragged by the outlier it is
    supposed to reject.
    """
    if len(session.gains_db) != len(config.input_channels):
        raise ValueError(
            f"session.gains_db has {len(session.gains_db)} entries but "
            f"{len(config.input_channels)} input channels are configured; "
            f"provenance must record a gain for each"
        )

    def make_sweep(duration_s: float) -> Sweep:
        return log_sweep(
            config.start_hz, config.stop_hz, duration_s, config.sample_rate_hz
        )

    idle = _verify_quiet(config) if config.verify_quiet else None

    if config.ramp:
        _run_safety_ramp(config, make_sweep, idle)

    sweep = make_sweep(config.duration_s)
    # Fault first, limiter second. `apply` normalises to unity peak before
    # scaling to the requested level, so whatever gain the fault carries is
    # normalised away and only its *shape* reaches the output -- a fault with
    # +12 dB in it cannot make the stimulus louder than it was asked to be.
    emitted = _with_fault(config, sweep.samples)
    stimulus = apply(emitted, config.level_dbfs, config.limit)

    passes = [_single_pass(config, sweep, stimulus) for _ in range(config.repeats)]
    has_reference = passes[0][1]

    provenance = Provenance(
        device=_device_name(config.device),
        sample_rate_hz=config.sample_rate_hz,
        gains_db=tuple(session.gains_db),
        timestamp=now or datetime.now(),
        cal_file=session.cal_file,
        cal_sha256=file_sha256(session.cal_file) if session.cal_file else None,
        temperature_c=session.temperature_c,
        coupling=session.coupling,
        setup_token=session.setup_token,
        injected_fault=config.fault.fingerprint() if config.fault else None,
    )

    notes = dict(session.notes)
    notes["repeats"] = str(config.repeats)

    results: dict[int, Measurement] = {}
    for channel in config.input_channels:
        stack = [aligned[channel] for aligned, _ in passes]
        impulse = _combine_passes(stack)
        spread = _pass_spread(stack, config.sample_rate_hz)
        results[channel] = Measurement(
            impulse=np.ascontiguousarray(impulse),
            provenance=provenance,
            arrival_samples=peak_index(impulse) if has_reference else None,
            timing=(
                TimingReference.LOOPBACK if has_reference else TimingReference.NONE
            ),
            repeat_spread=spread,
            notes=notes,
        )
    return results
