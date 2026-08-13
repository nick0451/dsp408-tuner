# Measurement theory

The methods this project uses, and — more usefully — where each one stops being valid.

## Log-swept sine (Farina method)

A logarithmic sine sweep is played through the system and the capture is convolved with a matched inverse filter. The result is the system's impulse response.

The reason for choosing this over MLS or an impulse:

- **Harmonic distortion separates out.** Because the sweep's instantaneous frequency rises logarithmically, each harmonic order deconvolves to a *distinct* arrival that appears **before** the linear impulse. Windowing discards them. Distortion does not contaminate the linear response, and it can be measured separately from the same capture.
- **High signal-to-noise for a given peak level.** Energy is spread over time rather than concentrated in one sample, so useful SNR is obtained at levels that do not endanger drivers.
- **Immunity to time-invariant noise.** Steady noise (engine, HVAC, road) does not correlate with the sweep and is suppressed by the deconvolution.

The sweep is *not* immune to time-*variant* noise. A door closing, a passing vehicle, or a rattle mid-sweep corrupts the measurement in ways that are not obvious in the resulting curve. Repeat measurements and compare.

### Sweep parameters

Longer sweeps give better SNR and better low-frequency resolution, at the cost of a longer window during which something can move. Start and stop frequencies should extend beyond the range of interest — the sweep's endpoints are where its energy distribution is least well-behaved.

The generated sweep is unscaled. Levelling happens in `tuner.safety`, which is the only sanctioned path to an output device.

## Deconvolution

FFT-domain convolution of the capture with the sweep's inverse filter. The inverse is the time-reversed sweep with an amplitude envelope compensating the log sweep's −3 dB/octave energy tilt, so the deconvolved result is flat for a flat system.

> **A deconvolution bug does not announce itself.** It produces a smooth, plausible curve that is simply wrong, and every downstream stage inherits the error. This is why the engine is validated against REW golden data rather than by inspection.

Impulse responses are real-valued `float64` with sample 0 at the **start of the captured buffer**, not at the acoustic arrival. The arrival offset is a separate, explicitly tracked quantity, and it is only meaningful when a loopback reference was captured.

## The timing reference

A hardware loopback — one interface output wired back to one interface input — establishes t=0 for the capture.

Without it:

- **Magnitude response, RT60 and spatial averaging remain fully valid.**
- **Delay and phase do not.** Absolute arrival time is unknown, so every derived delay is offset by an unknown constant and every phase curve carries an unknown linear term.

A USB microphone is on its own clock and cannot provide a hardware loopback. Measurements made with the UMIK-1 alone are magnitude-only.

`tuner.measure.result.Measurement` enforces this by raising `NoTimingReference` rather than returning a plausible number. A wrong delay figure is worse than no figure, because it gets applied to the DSP.

### Sequential measurement is phase-correct

Given a valid timing reference, measurements taken *sequentially* with a single microphone are phase-coherent with one another — each capture's absolute time reference is recoverable from the loopback. This is standard REW practice for time alignment and it works.

The phase-coherence problem people worry about applies to *simultaneous* capture across devices on **different clocks**. Multiple USB microphones have this problem; one interface with multiple inputs does not.

Consequence: additional microphones buy iteration speed, not accuracy. See `docs/hardware.md`.

## Gating

Windowing the impulse response rejects room and cabin reflections, approximating a free-field measurement.

The cost is low-frequency resolution, and it is unavoidable: **a window of length T gives valid data only above roughly 1/T.**

| Window | Valid above |
|---|---|
| 2 ms | ~500 Hz |
| 5 ms | ~200 Hz |
| 10 ms | ~100 Hz |
| 20 ms | ~50 Hz |

In a car, the first reflection typically arrives within a few milliseconds, so a window long enough to reach 100 Hz will already contain reflections. There is no gate setting that gives clean free-field data at low frequencies inside a vehicle. This is a physical limit, not a tuning problem.

Consequences:

- Below the gate limit, what is measured is the **in-car response including cabin gain and modes** — which is arguably what should be corrected anyway, since it is what the listener hears.
- For **microphone calibration**, where genuine free-field data is required, gating cannot be used at low frequencies at all. This is why the calibration rig uses a sealed coupler instead. See below.

A half-Hann taper is applied at the trailing edge; rectangular truncation produces spectral ripple.

## Spatial averaging

Combining measurements from several listening positions reduces the influence of position-specific modal nulls — a deep null at one microphone position is often a cancellation that moves a few centimetres away, and correcting it wastes headroom on a problem that is not general.

Two modes, which give **different answers**:

- **Complex averaging** — used when every input has a valid timing reference. Preserves phase relationships; nulls that are genuinely common across positions survive, position-specific ones cancel out.
- **Magnitude averaging** — used when no timing reference is available. Cannot distinguish a real null from a phase artifact.

Mixing the two within one average is rejected rather than silently coerced.

## The repeatability floor

Every acceptance decision is made against this number, so it has to be measured rather than assumed.

**Procedure.** At the start of each session, before any tuning, repeat one complete measurement several times without changing *anything* — do not move the microphone, do not touch a gain, do not open a door. Score each repeat with the session's frozen objective. The spread between the highest and lowest score is the floor.

Full spread is used rather than a standard deviation. With the three-to-five repeats that are practical in a car, the spread is the more honest bound on how far a single measurement might be off; a standard deviation from five samples implies a precision the sample size does not support.

**It is a per-session quantity and is never carried over.** The floor moves with:

- Temperature stability — a car warming in sun drifts continuously.
- Mounting rigidity — a mic on a flimsy stand moves between sweeps.
- Ambient noise — traffic, wind, HVAC, rain on the roof.
- Whether anyone is in the vehicle.

A floor measured in a quiet garage in the morning does not describe the same car in a car park at noon. `RepeatabilityFloor` carries a `session_id` so a carried-over value can be detected rather than silently reused.

**What a large floor means.** If the floor is comparable to the improvement being sought, the session cannot demonstrate success and no amount of optimization will change that. The correct response is to fix the measurement conditions — better mounting, wait for thermal equilibrium, find somewhere quieter — not to lower the acceptance threshold. A floor that is too large is information, not an obstacle.

**Why the threshold is not zero.** An improvement smaller than the floor is indistinguishable from having changed nothing. Accepting those is how a tune accumulates dozens of changes that collectively do nothing measurable while appearing, step by step, to be progress.

See the improvement invariant in CLAUDE.md and `tuner.optimize.verify`.

## Substitution calibration

Derives a calibration curve for an uncalibrated microphone by comparing it against a reference in an identical sound field:

```
Cal_DUT(f) = Cal_REF(f) + [Mag_REF(f) − Mag_DUT(f)]
```

This is what commercial calibration services do; they differ only in using a laboratory reference (B&K) rather than a UMIK-1.

Three things determine whether the result is trustworthy:

1. **Capsule position must match between the two measurements — not body position.** Microphone bodies differ in diameter. This requires a swap fixture; eyeballing it introduces exactly the kind of small positional error that changes high-frequency response.
2. **Orientation must be fixed at 0°.** Calibration curves are angle-dependent above roughly 8 kHz.
3. **Low and high frequencies need different methods.** See below.

### The two-method split

**Above ~250 Hz: gated free-field.** Gate out reflections, compare in a normal room.

**Below ~250 Hz: sealed coupler.** A small airtight cavity with a driver and two grommeted microphone ports. Below the cavity's first standing-wave mode, pressure is uniform throughout, so both capsules see an identical stimulus regardless of exact position — which is precisely the condition gating cannot deliver at low frequencies.

The cavity's usable ceiling is set by its first mode, a function of its largest internal dimension (`f₁ = c / 2L`). `tuner.cal.coupler.max_valid_hz` computes it with a safety factor. Exceeding it silently produces position-dependent data, defeating the purpose.

**A leak behaves as a high-pass filter** and will be mistaken for microphone roll-off — the exact quantity being measured. Verify the seal before trusting the data.

The two curves are spliced with a crossfade around 250 Hz, after aligning them by their offset in the overlap region.

### Error budget

The UMIK-1's own calibration file is itself a comparison calibration (~±1 dB), and the transfer adds perhaps ±0.5–1 dB. Derived microphones land around **±1.5–2 dB absolute**.

But **relative matching between array elements is far better than that figure suggests**, because mics calibrated against the same reference by the same procedure share their common-mode error. Spatial averaging and L/R asymmetry analysis depend on relative match, so the array is more accurate for its actual purpose than the absolute number implies.

## Validation

**What the engine is actually checked against, as of 2026-08-09:**

- **Analytic known answers.** A pure delay must deconvolve to an impulse at that sample; a designed biquad must reproduce its own magnitude and phase; a synthetic exponential decay must yield its own RT60. These are genuine predictions made outside the code under test, and they catch the whole class of deconvolution errors that produce smooth, plausible, wrong curves.
- **An electrical loopback**, flat to ±0.35 dB, with level linearity to 0.07 dB.
- **A physical known answer**: a measured LR4 crossover corner fits the textbook response, which exercises the full acoustic path rather than the maths alone.

> ### ⚠ The REW comparison does not exist
>
> This section previously stated that "the measurement engine is validated against REW: golden tests compare our frequency response to REW's export of the same stimulus and capture… Reference data lives in `tests/golden/`."
>
> **No such test was ever written and no such data was ever captured.** `tests/golden/` holds the DSP protocol fixture only. The claim stood in three documents for months.
>
> This matters because of what the existing checks cannot do. An electrical loopback is **our chain measuring our chain** — it confirms the two ends agree, not that either is right. The analytic tests confirm the mathematics, not the acoustics or the driver code. Neither is independent ground truth, and independent ground truth is exactly what a measurement engine needs, because every downstream stage inherits its errors silently.
>
> Building it is one bench session: capture a sweep, export the same capture from REW, store both, write the comparison. Until then, **no claim of independent validation for the measurement engine is available.** Tracked in `STATE.md`.

The calibration rig will provide a second known-answer test — calibrating a microphone that already has a factory calibration file should reproduce that file within the stacked error budget. `tuner.cal` is not built.
