# CLAUDE.md

Guidance for working in this repository.

**For current state, open questions and next steps, read [docs/STATE.md](docs/STATE.md).** This file holds the rules; that one holds the situation.

## What this project is

An automated acoustic tuning system for a **Dayton Audio DSP-408** (4-in / 8-out car audio DSP). It plays measurement stimuli through the system, captures them with calibrated microphones, derives impulse and frequency responses, fits corrections (gain, delay, crossover, parametric EQ), writes those to the DSP, and re-measures to verify convergence.

**This is a measurement and constrained-optimization system, not a neural-network one.** The core tuning path contains no learned components and must never require one. The hard parts here are signal processing (swept-sine deconvolution, gating, spatial averaging), numerical optimization (fitting biquads to a target under a finite hardware resource budget), and microphone calibration. Machine learning has three plausible future roles — a preference-learned target curve, an impulse-response pathology classifier, and a surrogate model to reduce physical measurement rounds — but all three are optional extensions layered on a system that already works without them.

## Hard safety rules

These are absolute. Violating them destroys hardware, usually a tweeter, usually irreversibly.

1. **Never emit a stimulus except through `tuner.safety`.** There is no "quick test" exception. Any code path that writes samples to an output device without passing through the safety limiter is a bug, regardless of what it is for.
2. **Every sweep starts at −30 dBFS and ramps.** Never jump straight to target level. The ramp exists so that a misrouted channel, a wrong gain setting, or an unexpectedly efficient driver is caught while it is still quiet.
3. **Abort on clipping or DC offset.** Both indicate the signal chain is not what the code thinks it is. Continuing under that assumption is how drivers die.
4. **Per-channel level ceilings are mandatory, and the default is the most conservative one.** A sustained full-range sweep into a tweeter will destroy it. When a channel's crossover is unknown — which is the case for every channel before the system has been characterized — treat it as a tweeter and use the lowest ceiling. Raising a ceiling is a deliberate act that requires knowing what is connected. In a tuning run that is `orchestrate.plan.DriverCeiling`, whose `basis` field is **required** and must name what is connected; it is recorded in provenance, so a ceiling that turns out wrong is traceable to the claim that set it.
5. **Never assume a channel is silent because you did not address it.** Verify routing by measurement, not by intent. In a tuning run that is `orchestrate.isolate`: mute every output but the one being swept, **read the mute states back from the device** rather than trusting the vendor app's display, and once per session mute *everything* and require the sweep to come back silent. That last check is the only one that can see a microphone hearing a path which never went through the DSP — the first two only prove the byte we wrote is the byte stored. A run that cannot mute (one output wired, nothing else connected) uses `NoIsolation`, which requires a written basis and lands it in provenance.
6. **The DSP's own gain is downstream of the safety limiter, and nothing in `tuner.safety` can see it.** The limiter caps what we *transmit*; the driver receives that plus whatever channel gain and EQ boost the device is configured with. A +12 dB band and a +6 dB channel gain turn a −18 dBFS stimulus into 0 dBFS at the speaker. **Subtract the device's gain from the stimulus level, deliberately, every time** — and prefer measuring with nothing connected to the output when a boost is involved. Identified 2026-08-09 while specifying the PEQ shape measurement; it is the one path by which a correctly-limited stimulus still arrives loud. **As of 2026-08-09 this is `safety.ceiling_for_device_state`, reachable as `DspBackend.stimulus_limit(output)`**, which reads the device live on every call — caching it is the natural optimisation and the wrong one, because the closed loop's whole shape is *write a boost, then sweep the channel you just boosted*.

## Units and conventions

Get these wrong and you lose days to a plausible-looking wrong answer. They are enforced by naming.

| Quantity | Internal representation | Notes |
|---|---|---|
| Phase | **radians** | Degrees only at display boundaries. Suffix display-side variables `_deg`. |
| Digital level | **dBFS** | Suffix `_dbfs`. |
| Acoustic level | **dB SPL** | Suffix `_dbspl`. |
| Delay / time offset | **samples** (int) | Milliseconds only at display boundaries; suffix `_ms`. |
| Frequency | **Hz** | Suffix `_hz`. |
| Frequency axes | **log-spaced** | State point count and range explicitly at every boundary. Never pass a bare array and hope. |

**Never use a bare `db` in a variable name.** `gain_db` is ambiguous between digital and acoustic and will eventually be read as the wrong one. Use `_dbfs` or `_dbspl`.

Impulse responses are real-valued, time-domain, `float64`, aligned so that **sample 0 is the timing-reference instant**. With a hardware loopback that is the reference arrival, so the interface's own latency is already removed and `arrival_samples` is simultaneously the arrival's index into the impulse *and* the propagation delay — one number, both meanings, which is what stops them drifting apart. Without a loopback, sample 0 is the deconvolution's zero-delay point and still carries unknown interface latency, which is exactly why delay and phase are unavailable in that case. See `Measurement` in `tuner/measure/result.py`.

**Device parameter units are not engineering units** — but as of 2026-08-08 the mappings are measured, so the `_raw` names are now conservative rather than necessary:

| Field | Mapping | Confirmed |
|---|---|---|
| `delay_raw` | integer samples at 48 kHz | app display, 5 channels |
| `gain_raw` | `dB = raw/10 − 60` | **Android** app display on 8 channels (it renders dB); **by measurement** (a 6.00 dB step measured 6.01 dB); and **by the ADAU coefficient** on 2026-08-11 (`gain_raw` 500 arrived as exactly −10.0000 dB in 5.23). Three routes. See the two-app note below before citing 'the app display' |
| EQ `level` | same encoding, 600 = 0 dB | app display, **and by measurement** (raw 720 produced +11.98/+11.99/+11.99 dB; raw 480 produced −12.02 dB) |
| EQ `bw` | `octaves = (raw + 5)/100`, **half-gain convention** | app display, **and by measurement** on raw 25/65/134 — the fitted half-gain width tracks the requested octaves to ±0.8 % across a 4.6× span |

> ### ⚠ There are **two** vendor apps and they display gain differently
>
> Operator, 2026-08-11:
>
> | App | Output gain | Input gain |
> |---|---|---|
> | **Android** (the debugging phone, and the HCI captures) | `−10.0 dB` | — |
> | **iOS** (what the operator normally uses) | **0–60**, i.e. `raw/10` | **0–100** |
>
> So **"confirmed against the app display" is ambiguous unless it names the
> app**, and every existing use of that phrase means Android, which does render
> dB. The `−60` offset claim was correct as written.
>
> This surfaced because a review flagged an apparent contradiction: the gain
> readback table showed operator values of 50/44/48 while the units table cited
> "app display" for a dB mapping. Both were literal; they were different apps.
>
> **The resolution was reached by asking, after first getting it wrong by
> inference.** Eight consistent operator readings against eight known
> `gain_raw` values gave a clean, wrong answer — "the app displays `raw/10`" —
> which was written into this file as a finding *in the same breath* as a note
> saying it deserved one question to confirm. Hedging one sentence while
> asserting in the surrounding paragraphs is not hedging. **If a claim is worth
> flagging as unconfirmed, it is not yet worth writing as a correction.**
>
> The offset does not rest on the display anyway: `gain_raw` 500 reached the
> ADAU1701 as a 5.23 coefficient of exactly −10.0000 dB. Three independent
> routes agree — the Android display, the wire protocol, and the DSP's own
> coefficient memory.
>
> **The iOS app reaches input gain, and the Android app does not.** `DataType 3`
> — the input section — is unmapped precisely because it never appeared in the
> Android captures. iOS displaying an input gain on a 0–100 scale is the first
> evidence that a vendor path to those parameters exists at all. If that section
> is ever needed, that is where to look.

Converters live in `tuner.dsp.protocol`: `gain_dbfs`, `gain_raw_for`, `bandwidth_octaves`, `q_from_bw_raw`, `bw_raw_for_q`. **Renaming the dataclass fields to engineering units is now permitted and pending** — it touches `protocol.py`, `sim.py` and their tests, so it is a deliberate refactor rather than a drive-by.

## The timing-reference rule

**No delay, phase, or group-delay figure may be reported from a measurement that lacks a valid loopback timing reference.**

A hardware loopback — one interface output wired back to one interface input — establishes t=0 for the capture. Without it, the absolute arrival time of the acoustic signal is unknown, so every derived delay is offset by an unknown constant and every phase curve carries an unknown linear term.

Magnitude response, RT60 and spatial averaging remain fully valid without a loopback. Delay and phase do not.

Measurement objects carry `timing: TimingReference` recording what established t=0. **The API must refuse to produce phase or delay results when nothing did** — raise, do not warn and return a number. A wrong delay figure that looks reasonable is worse than no figure, because it will be applied to the DSP.

### ✅ 2026-08-13: three states, because an acoustic reference is neither

A USB microphone is on its own clock and cannot provide a hardware loopback.
This file used to conclude from that "measurements made with it are
magnitude-only". **That was too broad**, and the operator's proposal is what
showed it: put the clapperboard in the air.

    generated (interface clock)
        REF_A ---- gap ---- measurement sweep ---- gap ---- REF_B
                   |<------ known interval, exactly ------>|

    captured (microphone clock)
        detect A                                        detect B

One loudspeaker plays a short chirp before and after the sweep; both are found
by matched filter. Two separate things fall out:

- **A common time origin.** Every measurement in a session refers to the same
  acoustic event through the same reference speaker, so arrivals are
  comparable *to each other* — including a subwoofer's, because the reference
  and not the subwoofer carries the timing. That is what makes delay alignment
  possible with a USB mic.
- **A clock-rate estimate.** Captured interval over generated interval is the
  ratio of the two clocks, over exactly the window that matters.

`TimingReference` is therefore `NONE` / `LOOPBACK` / `ACOUSTIC`, and the middle
state is the point:

| | absolute delay | relative delay, phase | magnitude |
|---|---|---|---|
| `LOOPBACK` | ✅ | ✅ | ✅ |
| `ACOUSTIC` | **refused** | ✅ *within one geometry* | ✅ |
| `NONE` | refused | refused | ✅ |

**Why absolute delay must be refused rather than approximated.** The reference
speaker's own path length is an unmeasured constant inside every arrival. It
cancels in a difference and never resolves on its own, so `arrival_samples`
stops meaning "propagation delay" — the identity that `Measurement`'s docstring
protects, and that a boolean flag would have quietly broken.

**Relative delay checks provenance, and that is not belt-and-braces.** The
constant cancels only while the microphone and the reference speaker stay put.
Two measurements either side of the mic moving have different constants, so
their difference is not a delay. `relative_delay_samples` calls
`require_comparable`, which means **the setup token now guards a delay written
to the DSP, not just a misleading curve.**

Three consequences that are not yet built and must be before this runs in a car:

1. **The reference output's delay and crossover must be frozen for the run.**
   The chirp passes through a DSP channel, so that channel's delay is inside
   the constant. Write a delay to it mid-run and t=0 moves for every
   measurement after, by exactly that amount, looking like an acoustic change.
   That is a plan-level refusal, not a note.
2. **The chirp is a stimulus.** Rule 1, no exception, and the reference output
   needs a `DriverCeiling` with a basis like any other. It fires twice per
   measurement, every measurement — the most frequently emitted signal in a
   run, and the one that should be limited hardest.
3. **The gap either side is not arbitrary**: it must exceed the room's decay,
   or the chirp's tail smears into the sweep.

#### The two references are different drivers, and we were conflating them

Researched 2026-08-13 against car-audio practice; see
[docs/time-alignment.md](docs/time-alignment.md) for sources.

| | **Timing reference** | **Alignment reference** |
|---|---|---|
| Chosen for | **Detectability** — a sharp arrival | **Physics** — a DSP can only *add* delay |
| Which | A tweeter. The reference is a 5-20 kHz sweep, so a **subwoofer cannot serve** | The **farthest** driver from the seat, which gets 0 ms |
| Typically | A front tweeter | Usually but *not always* the sub — never hardcode it |

`normalize_delays` already produces the second by subtracting the common
minimum. What was missing is that the first is a separate role the plan has to
name.

**REW's author says this method is unreliable in a car** — "many strong, close
reflections which can affect the determination of the reference time". Three
things follow, and the first two are already right:

- **Wide band beats low.** The correlation peak's width is what separates a
  direct arrival from a reflection. 5-20 kHz resolves 200 µs / 69 mm; the
  2-8 kHz we shipped first resolved 500 µs / 172 mm, which in a cabin is the
  difference between working and not. Changed the same day.
- **A consistently wrong detection is survivable; an inconsistent one is
  not.** A detector locked onto the same reflection every time is a constant,
  and a constant cancels in relative delay — which is all we report. **So the
  guard that matters is that the reference arrival repeats across a
  measurement's repeats, not that it is correct.** Not yet built.
- **Crossovers on the reference channel degrade detection**, not just offset
  it — the field disables them for timing. We cannot, so the band must sit
  clear of the reference tweeter's corner. Another reason 5-20 kHz.

**Polarity by the null, not the peak.** The field inverts the sub, re-measures,
takes the polarity giving the *deeper* null at the crossover, then uses the
opposite. A null is a sharp feature and a summation peak is a broad one — the
same reasoning behind differential measurement here.

#### Two things measurement settled that argument would not have

**Take the first *group*, not the first peak.** A matched filter's main lobe is
not a spike — its width is set by the reference's bandwidth and it ripples
inside that width. On this chirp, five local maxima sat above half the peak
across 18 samples, so a naive "first local maximum above threshold" read **9
samples early, every time**: 190 µs, 65 mm of apparent path. The guard is three
over the bandwidth, and it is a real physical limit rather than a knob —
**arrivals closer together than the inverse bandwidth cannot be separated at
all.** A 6 kHz reference resolves about 170 µs.

**The clock question could not be answered electrically.** Counting PortAudio
callback frames on both devices over 60 s quantises at one 480-sample buffer,
so it can only say "under 167 ppm" — and 167 ppm over a 2 s sweep is 16
samples, inaudible in magnitude and **3.3 cycles at 10 kHz** in phase. The
two-chirp interval is the only instrument that measures the composite ratio,
over the real window, including any resampling the host does out of sight.
Known-answer tested from 5 to 500 ppm, recovered to within 10.

## ADAU1701 resource budget

The DSP-408 is built around Analog Devices ADAU1701 SigmaDSP silicon. This is not a general-purpose processor with room to spare:

- **Fixed 48 kHz sample rate.** Not negotiable, not switchable.
- **Finite program space.** Every biquad, delay line and mixer node costs instructions. The per-chip instruction count is **uncited here on purpose**: this file's own rule forbids stating ADAU1701 figures from memory, and the figure that used to sit in this sentence had no source. Cite the datasheet revision when you need a number, or measure the ceiling.
- **Delay RAM is a shared pool.** A generous delay on one channel takes it away from others in the same pool. This is the constraint most likely to be forgotten, because the per-channel UI makes each channel look independent.

**A tune that does not fit the chip is not a tune.** The optimizer solves against the resource budget as a hard constraint, not as a post-hoc check. Producing an attractive target-matching curve that cannot be loaded is a failure, not a partial success.

> ### ✅ Settled 2026-08-11: two chips, two pools, measured
>
> **The DSP-408 contains *two* ADAU1701s**, so program space and delay RAM are **per chip**: two independent pools of four outputs, not one pool of eight.
>
>     ADAU at I²C 0x37  ->  outputs 1, 2, 3, 4
>     ADAU at I²C 0x35  ->  outputs 5, 6, 7, 8
>
> Measured on a logic analyser tapping the ADAU control bus (2×5 header, pin 10 = SCL, pin 9 = SDA). Each output was stepped one gain click and the chip that received the write recorded. `DeviceLimits.output_chip` carries the map; `SimulatedDsp` and `optimize.budget` enforce per-chip pools.
>
> **The grouping is measured; the pool size is not.** `delay_samples_per_chip` is 1024, chosen to preserve the old single-pool placeholder's device total of 2048 rather than doubling it — splitting a placeholder must not quietly widen the model. It stays `measured=False`.
>
> Three things worth keeping from how this was settled:
>
> - **The obvious guess was right, and measuring it was still worth doing.** Outputs 1–4 / 5–8 had been the obvious reading for weeks and the project refused to write it down. When the measurement landed exactly there, that was the moment to distrust the attribution *hardest*, not least: four of the eight outputs had been stepped with identical values, so their assignment rested on the order they were edited in. Re-running those four in **reverse order** produced the same channel-to-chip assignment in the opposite sequence position. That is what turns a coincidence into a result.
> - **Per-chip pooling does not license per-chip delay normalisation.** Two pools make `normalize_delays` look like it should subtract each chip's own minimum. It must not: that shifts one chip's drivers in time relative to the other's, and the subwoofers on chip 2 have to stay aligned with the mids on chip 1. Pooling constrains what *fits*; it is not permission to move drivers.
> - **A device total is not a budget.** `BudgetUsage.delay_headroom_samples` reports the **tightest** chip, not the sum. A sum would show 900 samples free while a tune is already unloadable because all 900 are on the wrong chip, and it would read as comfortable margin right up until the write failed.


> ### ✅ Settled 2026-08-12 by asking: the resource limits are UI facts
>
> Operator, from the vendor app. **Both numbers close an open question that had
> been costed at a bench session each.**
>
> | | |
> |---|---|
> | **Max delay, per channel** | **8 ms**, shown also as 277 cm / 109 in |
> | **PEQ bands, per channel** | **10**, not the 31 the protocol addresses |
>
> 8 ms × 48 kHz = **384 samples**, and 277 cm ÷ 343 m/s = 8.08 ms. Three units
> agreeing is also a third independent corroboration that `delay_raw` is
> samples at 48 kHz.
>
> #### The delay pool is probably not a pool
>
> If the app offers the full 8 ms on **every** channel regardless of what the
> others use, the allocation is static per channel, not shared: a shared pool
> would have to shrink one channel's maximum as its neighbours consumed it.
> That makes the per-chip figure 384 × 4 = **1536 samples**, statically
> divided, rather than 1024 shared.
>
> `DeviceLimits.delay_samples_per_chip = 1024` is then wrong in **both**
> directions — it would permit 1024 samples on a single output the device caps
> at 384, and refuse a four-channel total of 1200 the device would run.
>
> This also inverts a warning in `CLAUDE.md`. "The constraint most likely to be
> forgotten, because the per-channel UI makes each channel look independent"
> should probably read: the per-channel UI makes each channel look independent
> **because it is**.
>
> **Confirmed 2026-08-12: 8 ms can be set on every output simultaneously.**
> There is no shared delay pool. The allocation is static per channel: 384
> samples each, 1536 per chip, and one channel's delay never takes from
> another's. `DeviceLimits.delay_samples_per_chip = 1024` and the pooled
> accounting in `optimize.budget` are both wrong and neither has been changed
> yet.
>
> #### `max_peq_per_channel = 10` was right, for the wrong reason
>
> The value is correct and should be marked **measured**, sourced to the app.
> Its current justification is not:
>
> > *"the device addresses 31 EQ bands per output. Two ADAU1701s will not run
> > 31 biquads on each of eight channels, so addressable is not affordable"*
>
> That is an **uncited ADAU1701 claim stated from memory**, in a project whose
> rule forbids exactly that — the same defect as the "1024 program
> instructions" line cut earlier the same week. The real limit is that the
> vendor exposes ten.
>
> **Consequence not yet guarded: bands 10–30 must never be written.** They are
> safe today only because they read flat and the EXCLUSIVE flatten loop skips
> flat bands — an accident, not a guard. `ADDRESSABLE_BANDS = 31` is used as
> both the fit range and the flatten range; both should be 10.
>
> #### What is actually left
>
> Not pool sizes. Just: **what does the device do with an out-of-range delay?**
> Refuse, truncate, or disturb a neighbour are very different futures. One
> bounded bench write answers it.

Exact figures for program space and delay RAM are **not measured and are not pending anything scheduled.** M0 is closed and did not produce them; they now need either an SWD dump of the MCU, a cited ADAU1701 datasheet figure, or a bench experiment that finds the ceiling by binary search. Until then `tuner.optimize.budget` uses conservative placeholders, clearly marked with `measured=False`. Do not treat them as authoritative and do not silently widen them. **Saying they "pend the M0 spike" hid the fact that they are orphaned; they have no owner until one of those three routes is chosen.**

## Rig verification: four preconditions

All four catch failures that produce smooth, plausible, entirely wrong curves
rather than anything that looks like an error. Three run automatically inside
`capture_sweep`; the fourth does not, and that gap is deliberate but real.

| Precondition | Enforced where | Raises |
|---|---|---|
| Idle noise floor is low | `capture_sweep`, before any stimulus | `NoisyPath` |
| Stimulus actually arrives | `capture_sweep`, during the safety ramp | `SilentPath` |
| Median of N repeats | `capture_sweep`, always | — |
| Gain is level-independent | **caller must invoke** `require_linear_path` | `NonLinearPath` |

**The linearity check is not automatic.** It costs ~14 s, which is too slow per
sweep but trivial per session. Until that is wired in, running it is the
operator's responsibility — and a run that skips it is not verified, whatever
the curve looks like.

**Never select an audio device by index.** MME lists the host's default output
first, so indices renumber when the default changes. On the bench this pointed
a measurement at the PC's speakers while still capturing the correct input,
yielding a smooth curve made of noise. Use a host-API-qualified name:
`'Speakers (Scarlett Solo USB), Windows WASAPI'`. Name lookup fails loudly when
ambiguous; index lookup fails silently with the wrong hardware.

**Prefer WASAPI to MME**, measured 2026-08-09: identical curve to 0.236 dB, but
2–5× less run-to-run scatter from 250 Hz to 10 kHz. `tools/bench_golden.py`
defaults to it and takes `--host-api`; the older bench tools still name MME and
should be moved over the next time each is run against hardware, each with its
own known-answer check.

**Check level-linearity before trusting any frequency response.**
`tuner.measure.qa.measure_level_linearity` plays tones at several levels and
confirms gain is constant; `require_linear_path` raises if it is not. A
compressor, limiter, noise gate or AGC anywhere in the chain invalidates
single-level measurements entirely. This is not hypothetical — Windows speech
noise-suppression on a capture endpoint once manufactured a convincing
70 dB/octave low-frequency rolloff here and corrupted four measurements before
a level sweep exposed it. In a vehicle the same trap is head-unit loudness
compensation or an amplifier limiter.

**Check the path is quiet, and that the stimulus reaches it.** A capture that
receives nothing still passes every other check — no clipping, no DC, a clean
noise floor — and deconvolving noise yields a smooth curve with plausible
features. Conversely, interference only 6 dB below the stimulus moved a
measured crossover corner by 6% while leaving the curve looking entirely
reasonable. Both were observed on the bench, not hypothesised; see
`docs/hardware.md`.

**Never characterize anything from a single sweep.** `CaptureConfig.repeats`
defaults to 3 and the passes are combined by median. One sweep cannot
distinguish a dropout from a real response feature; across repeats, artifacts
move and real features do not.

Two implementation details in `_combine_passes` that are load-bearing rather
than incidental:

- Passes are aligned to each other to **sub-sample** precision first. Latency
  drifts tens of samples between runs, and combining unaligned passes
  comb-filters the result into something that looks like a catastrophic system
  response.
- The median is taken **per frequency bin, not per time sample**. A dropout is
  narrowband, so in the time domain it is spread thinly across thousands of
  samples and is an outlier at none of them.

## Ambient noise, and what it can be mistaken for

Everything up to 2026-08-13 was measured down a cable, where the noise floor
is stationary and one probe before the sweep is a fair sample of the whole
capture. A room is neither. Two claims made here about that were **wrong**,
and both are recorded because both were plausible and neither survived
arithmetic.

> ### ⛔ Refuted: "low-frequency ambient hides in a broadband RMS gate"
>
> It does not. RMS is dominated by whichever band carries the most power, so
> LF ambient at −55 dBFS drags the broadband figure to −55 and
> `require_quiet_path` fires anyway. **A per-band gate was designed and then
> dropped**, because measurement showed it would add almost nothing over the
> check already there.
>
> The real limitation of that gate is different and worth knowing: it compares
> the floor against the **transmitted** stimulus level, not the received one.
> Down a cable those are within a few decibels. Acoustically the received level
> is tens of decibels lower and frequency-dependent, so the gate is a statement
> that *the input path is quiet*, not that *the measurement has margin*. Both
> are worth having; only the first is what it returns, and `snr_db` now says so.

> ### ⛔ Refuted: "room noise makes a linear path read as compressed"
>
> Not from broadband ambient. `measure_level_linearity` reads a **single FFT
> bin** at the tone, over a 1.1 s window — a 1.36 Hz effective noise bandwidth
> against 24 kHz, which is **42.5 dB of rejection**. A tone captured at
> −70 dBFS is only troubled once the room reaches about −27 dBFS *at the
> input*, which is a fault rather than a room.
>
> That number had already been computed in the same conversation and was then
> ignored while writing a test that contradicted it. **An arithmetic result
> that is not carried into the next step is not a finding, it is a note.**

> ### ✅ What is real: narrowband interference on a test frequency
>
> Mains harmonics (300 Hz is the fifth of 60), fan blade tones, motor whine.
> One bin has **no rejection at all**, and an interferer that does not scale
> with stimulus inflates a tone's level most where the tone is weakest — so
> gain falls as level rises, which is the signature of compression.
>
> **`LinearityResult.usable()` structurally cannot see it.** That test drops
> tones which are too *quiet* relative to their neighbours; an interferer makes
> a tone too *loud*. It was built for a stopband tone through a filtered
> channel and it is right for that; it is blind to this by construction, not by
> tolerance.
>
> `usable_against(idle)` judges each tone against the floor measured at its own
> frequency. `IdleNoiseResult` keeps its spectrum so it can answer that, and
> `AcousticMeasurer` **requires** an idle floor for an acoustic session and
> permits its absence for an electrical one — the same asymmetry as
> `setup_token`, for the same reason.
>
> **The margin it asks for is derived, not chosen.** A floor `x` dB below a
> tone inflates it by `10·log10(1 + 10**(-x/10))`:
>
> | margin | induced error |
> |---|---|
> | 6 dB | 0.97 dB |
> | 12 dB | 0.27 dB |
> | **20 dB** | **0.043 dB** |
> | 40 dB | 0.0004 dB |
>
> `DEFAULT_MIN_TONE_MARGIN_DB = 20.0`. Reusing the relative test's 40 dB was
> four times stricter than the arithmetic asks, and it showed: it excluded
> every tone in a room quiet enough to measure in. **Two constants that look
> like the same idea can have one derivable answer and one judgement call.**

> ### ⚠ A property that recomputed its own mask, and so ignored the caller's
>
> Found while wiring the above. `require_linear_path` computed a mask for the
> "enough usable tones" check and then judged linearity with
> `result.spread_db` — a **property** that internally calls `usable()` and
> therefore rebuilt the *relative* mask, discarding whatever had just been
> decided.
>
> Invisible for as long as the two agreed, which they always did, because both
> called `usable()` with the same default. Wrong the instant they diverged —
> which is exactly what supplying a floor does. The fix is
> `spread_db_of(mask)`, and the lesson is that **a convenience property which
> recomputes an input is a trap for the first caller who supplies that input
> explicitly.**

> ### ✅ The only instrument that sees noise *during* the sweep
>
> `PassSpread`, on every `Measurement`. `capture_sweep` already took repeats,
> aligned them and medianed them per frequency bin; their **disagreement** was
> already being computed and thrown away. It is a direct per-bin measurement of
> how much the environment moved while the measurement was being taken — which
> the idle probe, taken beforehand, is blind to. Electrical noise is stationary
> so a snapshot was a fair sample; ambient noise is bursty and it is not.
>
> Two limits stated rather than left to be discovered:
>
> - At three passes, **two contaminated ones beat the median.** The spread
>   still reports the disagreement, so the failure is visible — but visible is
>   not corrected.
> - A *sustained* change contaminates every pass equally and produces **no
>   spread at all.** HVAC that switches on before the first repeat and stays on
>   is invisible here, and belongs to the idle check.
>
> Magnitudes only. A residual alignment error is a phase ramp, and including
> phase would report it as noise — growing with frequency, exactly where this
> rig is already least validated.

## Injecting a fault, so a bench run has a right answer

`tuner.measure.fault.FaultFilter` filters the generated sweep **before** it is
emitted, while the deconvolution still runs against the **unfiltered** sweep.
The measured response is then `H_fault × H_system`, so the fault is part of
the system as far as everything downstream can tell, and the tuner has to find
it by measurement. `fault.response_db()` is the answer; a perfect correction is
its exact negative, which makes a bench run scoreable against **zero**.

Three reasons for injecting *there* and not somewhere more obvious, none
interchangeable:

- **A fault written into the DSP as an EQ band would be subtracted at fit
  time** by `TuneRun._without_existing_eq`, so the tune would be right by
  arithmetic rather than by hearing anything.
- **A Windows APO would silently not apply.** System-wide equalisers live in
  the shared audio engine, and this project opens its output in **WASAPI
  exclusive** mode to reach 48 kHz alongside a UMIK-1. The run would correct
  nothing and look entirely reasonable.
- **`safety.apply` normalises to the requested level after the fault**, so a
  fault carrying +12 dB shapes the stimulus but cannot raise it. The ramp
  carries the fault too — a ramp probing an unfaulted signal verifies a chain
  the measurement never uses.

`Provenance.injected_fault` carries a fingerprint of the **label and the
coefficients**, checked before the environmental terms. A label that stayed put
while the filter moved is precisely the comparison that must not pass. A
faulted capture is incomparable to a clean one, so a bench run is internally
consistent and cannot masquerade as a real measurement.

## A clipped input now says what to do about it

It used to say `clipping detected on channel 1 (peak 0.9999)` — true, and
useless. It now reports the peak in dBFS, the headroom, and **how many samples
were at full scale**, because a handful and a percent want different responses:
a handful is a transient and the sweep is worth repeating; a percent is a chain
running hot and repeating it will fail the same way.

It also names the knob. For a clipped *capture* that is the interface's input
gain — the stimulus level is already ours and already limited, and lowering it
instead buys headroom by giving away signal-to-noise, **which is the wrong
instinct precisely because it also appears to work**.

`inspect_capture()` is public so the bench tools can report headroom before a
sweep is lost to it. `tune_run measure` prints it and flags both directions:
under 6 dB is tight, and **over 30 dB is also wrong** — that is converter range
thrown away, and it surfaces as a coarser repeatability floor rather than as
anything resembling a fault.

> ### ⛔ Every acoustic stimulus played an octave high, and nothing noticed
>
> Found 2026-08-13, the first time this rig produced sound. **The single most
> dangerous class of bug this project has hit**, because the capture it
> produces is clean by every measure the code has.
>
> `play_record` builds its playback buffer as `max(out_channels) + 1` columns
> wide. Measuring output channel 0 gives **one** column. The split path then
> opens the output stream at the buffer's width — and a WASAPI stream in
> **exclusive** mode performs no format conversion, because that is what
> exclusive mode *is*. Handed a mono buffer, a two-channel device reads the
> samples as interleaved stereo frames and consumes them two at a time.
>
>     sent 1000 Hz  ->  heard 1984.2 Hz   (buffer channels=1)
>     sent 1000 Hz  ->  heard 1000.0 Hz   (buffer channels=2)
>
> Confirmed as a ratio, not a fixed tone, by sweeping the request: 700 → 1403.3
> and 1400 → 2806.0, a consistent factor of 2.
>
> #### Why every existing precondition passed
>
> This is the part worth keeping. The four rig-verification checks, the safety
> limiter and the deconvolution all had nothing to say:
>
> | check | why it was blind |
> |---|---|
> | idle noise floor | measured with no stimulus; unaffected |
> | stimulus arrives | a signal *did* arrive, at a healthy level |
> | median of repeats | every repeat is wrong in exactly the same way |
> | level linearity | gain is level-independent at the wrong frequency too |
> | clipping / DC | levels are untouched — the samples are the same values |
> | wall-clock rate | **both streams measured correct**, 47 839 and 47 957 Hz |
>
> The capture was a clean, steady, full-duration tone at a plausible level.
> A sweep through it deconvolves into a smooth, entirely plausible frequency
> response — of a system shifted one octave.
>
> **The level is right and the spectrum is not**, which is why this is not a
> failure of `tuner.safety` and is still safety-relevant: a sweep band-limited
> for a particular driver arrives an octave away from where it was aimed. Rule
> 6's shape again — a correctly-limited stimulus arriving somewhere the limiter
> cannot see.
>
> #### The guard, and why it had to be a new kind of check
>
> Every precondition in `measure.qa` asked whether the signal was **clean**.
> None asked whether it was **the signal we sent**. `measure_tone_roundtrip` +
> `require_correct_timebase` now do: one tone, under two seconds, and it
> searches the whole audible band for the loudest bin rather than a window
> around the request — a window would find the largest peak *near* where the
> tone was meant to be, which is the assumption under test.
>
> It has a third outcome. Below `DEFAULT_ROUNDTRIP_MARGIN_DB` the peak is the
> room's, so it raises `IndeterminateTimebase` rather than passing.
>
> Three general lessons:
>
> - **A known-answer test on the rig is not the same as a known-answer test on
>   the maths.** This project had analytic tests, golden frames and a REW
>   comparison, and none of them ran through the acoustic path.
> - **The wall-clock rate check was the wrong instrument and looked like the
>   right one.** Both streams delivered the right number of frames per second.
>   The fault was in how the frames were *interpreted*, which no rate
>   measurement can see.
> - **Reasoning produced three wrong diagnoses in a row** — a partial capture,
>   an independent room tone, a clock ratio — and each was plausible. What
>   settled it was sweeping the requested frequency and looking at the ratio,
>   which took one minute.
>
> The fake could not have caught this either: `FakePortAudio.query_devices`
> reported whatever was asked for, so a channel-count mismatch was
> inexpressible. It now carries `device_output_channels` / `device_input_channels`,
> defaulting to 2. *Same lesson as the padding bug below, one layer out: a test
> double that cannot represent the hardware's constraints cannot fail the way
> the hardware does.*

> ### ⚠ Padding a short capture hid an all-zero measurement
>
> Found 2026-08-13, first time the split-clock path met hardware.
>
> `_play_record_split` reads the input stream's frame count to learn where
> playback began, *before* `write()` returns. A cold WASAPI exclusive open can
> take a long time, and when it does the capture window lands past the end of
> the recording. The code padded the shortfall with zeros and returned.
>
> **An all-zero capture deconvolves into a smooth, entirely plausible
> frequency response.** Nothing downstream can tell. It was seen live: one
> probe returned `-inf dBFS peak` and the next, seconds later, returned real
> ambient through the same cable.
>
> It refuses now, naming how many frames are missing. About one buffer is
> still padded -- the capture does legitimately close a moment after the last
> frame plays -- and anything larger is an error.
>
> Two things worth carrying:
>
> - **Padding is a decision to fabricate data.** It is nearly always dressed
>   as robustness. Here the fabricated value was silence, which is the one
>   value the measurement chain cannot distinguish from a valid result.
> - **The fake could not reproduce it** until it was taught to deliver *fewer*
>   frames during playback than were played. It had been written to always
>   return exactly as many as it was given -- the healthy case, and only the
>   healthy case. *A test double that models only success cannot fail the way
>   hardware does.*

## Measurement provenance

Every stored measurement records: microphone calibration file (path and hash), interface and device identity, all gain settings, sample rate, timestamp, and ambient temperature.

This is not bookkeeping. **A car's acoustic response moves measurably with temperature**, and gain settings that changed between sessions will masquerade as an acoustic difference. Measurements lacking provenance cannot be compared across sessions and the code must refuse to diff them rather than producing a misleading delta.

## The device is never inside the optimizer's inner loop

**Fit offline. Write once per round.** Every parameter write goes straight to
non-volatile storage on an MCU with finite endurance, so a fitter that wrote a
candidate per iteration would spend tens of thousands of writes on states
nobody ever measures. The optimizer evaluates candidates against its *model*;
only the chosen configuration reaches the device.

This costs nothing — the fit is a model-space search and gains nothing from a
round trip — but it has to be stated, because "write it and measure" is the
obvious shape for a tuning loop and it is the wrong one here. Compute is not
the constraint (a 10-band fit is ~5.4 s per channel); write endurance and
measurement time are.

Endurance figures for the part are **not known** and must not be invented. The
rule stands on the argument, not on a number.

> ### ✅ 2026-08-11: the model matches the device, measured
>
> The default failure mode of this class of tool is an optimizer that
> converges beautifully against a model of a system it has stopped resembling.
> `tools/tune_run.py predict-check` is the experiment that rules it out, and it
> now has a number.
>
> One known band written **through our own backend** — not the vendor app —
> then measured differentially: sweep with the band flat, sweep with it set,
> take the difference. That cancels the interface, the cabling and the
> channel's own crossover exactly, which a single sweep cannot.
>
> | | |
> |---|---|
> | requested | 1000 Hz, −6.0 dB, Q 2.00 |
> | achieved after quantisation | 1000 Hz, −6.00 dB, Q 1.983 |
> | measured notch depth | −6.08 dB |
> | predicted notch depth | −6.00 dB |
> | rms error, 450–3500 Hz | **0.065 dB** over 300 points |
> | max error | 0.298 dB |
> | mean offset (level drift) | −0.002 dB |
> | **session repeatability floor** | **0.0585 dB** |
>
> **The disagreement is the noise floor.** Every link is covered at once:
> `Biquad` → `_band_to_eq` → protocol encoding → RFCOMM → ADAU execution →
> analogue output → deconvolution → `biquad.response_db`.
>
> Three details that are load-bearing rather than incidental:
>
> - **The prediction uses the *achieved* parameters, not the requested ones.**
>   Bandwidth is quantised and `bw_raw_for_q` rounds up, so Q 2.00 becomes
>   1.983. Predicting from the request would have folded a known quantisation
>   into the error term and made the agreement look worse than it is.
> - **A cut, not a boost.** A boost lowers the stimulus ceiling (hard safety
>   rule 6), so the second sweep would have to run quieter and the differential
>   would then contain the level change as well as the filter. The tool refuses
>   a positive `--band-db` for that reason.
> - **The prediction is evaluated at 48 kHz, the interface at 44.1 kHz.** The
>   biquad runs in the ADAU, so its response must be computed at the ADAU's
>   rate; biquad response warps near Nyquist and using the interface's rate is
>   a silent error that grows with frequency. Those two numbers being different
>   on this bench is a feature — it made the distinction impossible to ignore.
>   Hardcoding 48 kHz for the *interface* is what failed first, with
>   `Invalid sample rate`.

> ### ✅ Fixed 2026-08-12: the fit now subtracts the EQ the channel already has
>
> `TuneRun._without_existing_eq`. The run still measures its baseline with the
> channel's real EQ in circuit — that is what the improvement invariant has to
> compare against — and the **fit** is given `measured − model(existing)`, so
> it solves for the complete new chain that an `EXCLUSIVE` write will install.
>
> **Why subtract a model rather than flatten and re-measure.** Flattening also
> yields a raw response and needs no model at all, but it destroys the
> baseline: "better than the operator's tune" quietly becomes "better than no
> EQ". Keeping both would cost **two sweeps per source**, and measurement time
> is one of the two real constraints here. Subtracting gets both from one
> sweep.
>
> What licenses trusting the model is that we measured it: `predict-check`
> put one band through the production backend and compared it against
> `biquad.response_db` at **0.065 dB rms against a 0.0585 dB floor**. Before
> that measurement existed, flattening would have been the right choice.
>
> **The residual assumption is the 30-band question.** Subtracting assumes
> every band `read_channel` reports is actually executing. Beyond
> `max_peq_per_channel` nobody knows, so the run refuses rather than guessing
> — at the fit, before anything is measured against a model it cannot justify.

> ### ⚠ The objective is nearly blind to narrowband error, and that is why this survived
>
> Found while writing the regression test, by reverting the fix and measuring
> what each candidate assertion actually saw:
>
> | | broken | fixed | a threshold that tolerates two stochastic fits |
> |---|---|---|---|
> | objective score gap | 0.233 dB | 0.052 dB | 0.3 — **passes broken** |
> | rms deviation between runs | 0.735 dB | 0.297 dB | 0.75 — **passes broken** |
> | **peak** deviation | **6.30 dB** | 1.06 dB | 2.5 — catches it |
>
> **A 6.3 dB error in the response moved the score by 0.23 dB.** An rms over
> 200 log-spaced points averages a narrow error away almost completely, so the
> first two assertions were vacuous: they would have shipped green over the
> exact defect they were written to catch.
>
> This is not a fact about the test. **It is why the improvement invariant
> reported `accepted` on both the broken and the fixed run**, and it
> generalises past this bug: *the scalar the run optimises cannot police
> localised error.* Anything that can go wrong in one narrow band — a
> mis-fitted filter, a band written to the wrong slot, a quantisation the
> model missed — is nearly invisible to it.
>
> Two consequences worth carrying:
>
> - **A regression test for a narrowband defect must assert on peak, or on the
>   defect's own signature, never on the objective.** The sharpest test here
>   asserts that no fitted band sits within half an octave of the preloaded
>   one carrying more than 3 dB — 6.3 dB broken, 0.5 dB fixed, no tuning of
>   thresholds required.
> - **The improvement invariant is necessary and not sufficient.** It catches a
>   tune that is broadly worse. It will accept one that is badly wrong in a
>   narrow band, which is the failure mode a wrong filter produces.

> ### ⛔ ~~Open defect~~ FIXED: the fit double-counts EQ the channel already has
>
> **Found 2026-08-11 offline, before the closed loop ever ran on hardware.**
> Not fixed. Read this before running `tuner.orchestrate` against anything.
>
> `TuneRun` measures the baseline with **whatever EQ the channel is already
> running** — nothing flattens or bypasses it, and the only write in the whole
> run is the WRITE stage. It then calls `biquad.fit(measured, target)`, which
> returns bands to be **added** to `measured`. It then writes them under
> `EXCLUSIVE`, which **replaces** every band on the channel.
>
> So the existing EQ is counted twice: once inside the measurement the fit was
> solving against, and once again by being deleted.
>
>     response after  =  raw + fitted          (what the device does)
>     fit solved for  =  raw + existing + fitted = target
>
> **Demonstrated.** A channel pre-loaded with +8 dB at 1200 Hz, against an
> identical run starting flat: the fit spent a −6.2 dB band at 1132 Hz
> cancelling a boost the write then removed, and the two runs' final responses
> differ by **5.9 dB** in band. **Both runs reported `accepted`.**
>
> The improvement invariant is not violated — the verdict compares against a
> baseline that genuinely was worse, and a real re-measurement was taken. It
> simply does not catch this: the tune improves on the baseline while landing
> several dB from where the same run starting flat would land. **Every channel
> on this car has EQ loaded**, so a first real tune meets it immediately.
>
> #### How this was nearly missed, twice
>
> The rehearsal never sees it because `FakeDsp408` starts with every band
> flat — the same blind spot as the Stage 6 gang test, one level up and
> semantic rather than structural. `--fake-from` would expose it.
>
> And **the first experiment appeared to refute it.** Pre-loading a −8 dB
> *cut* produced almost no divergence, which read as a clean refutation. The
> cause was `FitConstraints.max_boost_db = 3.0` with a 4× boost penalty: the
> fitter is deliberately, correctly boost-averse, so it never attempted the
> +8 dB correction that would have exposed the double-count. Re-running with a
> pre-existing **boost**, which the fitter *is* allowed to cut, showed the full
> effect. *An experiment that cannot produce the symptom is not evidence of its
> absence* — and a constraint that makes a component well-behaved can make a
> bug in its caller invisible.
>
> #### Two coherent fixes; the choice is not obvious
>
> 1. **Flatten the channel's EQ before the baseline sweep.** Measures reality
>    with the EQ out of circuit, which is what a human tuner does. Costs a
>    write and a restore per channel, and the run briefly leaves the car
>    unequalised.
> 2. **Subtract the loaded EQ's modelled response before fitting** —
>    `raw = measured − response_db(live.peq)`. No extra writes, and newly
>    defensible: `predict-check` measured that model against the device at
>    **0.065 dB rms**, at the repeatability floor. It inherits whatever the
>    model cannot express, which for a shelf or a contradicted block is not
>    nothing.
>
> Whichever is chosen, `ChannelConfig.peq` from `read_channel` is the wrong
> input on its own — it compacts non-flat bands to a leading run, so it
> describes the right *filters* at the wrong *indices*.

## The improvement invariant

**No tune is accepted unless a fresh measurement shows it improved the objective by more than the measurement repeatability floor.**

The optimizer's own predicted improvement does not count and is not evidence. Only a physical re-measurement, taken after the settings were written to the DSP, can accept a result. An optimizer that converges beautifully against a model of a system it has stopped resembling is the default failure mode of this entire class of tool, and prediction-versus-measurement is the only thing that catches it.

Four qualifications, each closing a specific way the rule gets satisfied without being met:

1. **The objective is defined and frozen before the run.** A multi-seat, multi-channel tune has conflicting objectives — improving the driver's seat can worsen the passenger's. If the scalar objective or its weighting may be chosen after seeing results, "improved" becomes trivially satisfiable by re-weighting, and every individual step still looks honest. Record the objective and its weights in the run's provenance.

2. **The threshold is the measured repeatability floor, not zero.** Establish it at the start of each session by repeating one measurement several times without changing anything; the spread is the floor. An improvement smaller than that floor is noise, and accepting it is how a tune accumulates changes that do nothing. Repeatability is a per-session quantity — it moves with temperature stability, mounting, and ambient noise — so it is measured, never assumed from a previous session.

3. **Failure means automatic rollback, not merely refusal to accept.** On non-improvement the previous settings are restored and the rollback verified by re-measurement. A tuning run that aborts partway must leave the system no worse than it found it. This is the operative half of the rule; without it a failed run silently degrades the car.

4. **The invariant governs the accepted result against the baseline — not every intermediate step.** Time alignment applied before EQ can legitimately worsen a magnitude objective while being necessary to the final result. Constraining each step forbids correct intermediate states and will drive the optimizer into local minima.

There are three outcomes, not two. If provenance is not comparable between the baseline and the verification measurement — temperature drifted, a gain was bumped, the cal file changed — the result is **indeterminate**, not pass or fail. Report it as such and roll back. Collapsing indeterminate into either bucket is how a temperature-driven change gets recorded as a tuning success.

Enforced by `tuner.optimize.verify` for the decision and by
`tuner.orchestrate` for the run; see `docs/measurement-theory.md` for how the
repeatability floor is established.

### Freezing the objective means fingerprinting it

Qualification 1 above is the one that fails silently, because the way it fails
is not dishonesty. It is looking at a disappointing result, noticing the
driver's seat did improve, and re-weighting toward it. Every individual step of
that is defensible and the run still reports "improved by more than the floor".

So the objective is hashed — target points, axis, band, and every weight — at
plan time, and re-hashed **immediately before the scores are computed**.
Re-weighting changes the hash and the run refuses to report the comparison.

**The placement is load-bearing and was wrong first.** The check originally sat
at the top of the verification stage, which looked equivalent and was not: the
verification sweep is the longest operation in the run, and in a rig that
prompts an operator between seats it is exactly when someone is sitting in
front of the results so far. A check before the sweep leaves the whole sweep as
a window. The test written to prove the freeze worked is what found this.

### A tuning run must own every band on the channels it tunes

`Dsp408Spp` takes a `PeqPolicy`, and `tuner.orchestrate` **refuses to start on
anything but `EXCLUSIVE`**. Under `LEADING` the fit writes bands 0..n-1 and
leaves the rest as it found them, so a fit with fewer bands than the previous
one leaves the surplus bands running. Every layer still looks correct — the
writes succeed, the readbacks match, the fit is good — and the verdict is about
a system that was never configured as predicted.

### `None` is not `False` in a rollback report

An abort can *be* the measurement path failing. Re-measuring after that
rollback is not evidence of anything, so the report records "not checked"
rather than a verification it did not make. Collapsing the two is the same
error as collapsing indeterminate into pass or fail, one level down.

### The objective we can honestly score today is shape, not level

`MagnitudeObjective` scores rms deviation from the target *after* the target is
raised to the measurement's own level. That is deliberate: our magnitude is
dBFS, not dB SPL, so an absolute-level objective would be scoring the
interface's input gain — which changes between sessions and is exactly what the
provenance rules refuse to compare.

Channel gain is still set, from `level_offset_db`. **Its correctness is not
evidenced by the verdict**, and no report should imply otherwise. Closing that
needs `tuner.cal`.

## DSP backend contract

All DSP control goes through the abstract interface in `tuner/dsp/backend.py`. Implementations:

- **`sim.py` — the default.** A simulated DSP. Desktop development and the entire test suite run against it. No hardware is required to work on any part of this project except the hardware backend itself.
- **`dsp408_spp.py`** — the real device, in engineering units. **It reads the real DSP-408, writes to it, and rolls the write back.** Proven on hardware 2026-08-11 in three steps that are worth keeping distinct, because the margin between them was the safety story: reads (31/31, firmware `MYDW-AV1.06`, all eight records, zero resyncs); the write **path** (Stage 4's no-op — 8 bytes to OUT1 block 31 carrying the device's own payload, fragmented 24 → 2×20 with 16 pad bytes, acked `0x51`, device byte-identical); and the write **effect** (Stage 5 — `gain_raw` 500 → 490, confirmed by readback, neighbouring bytes in the same block preserved, then restored and re-read). **Proven through Stage 6 on 2026-08-11**: whole-channel `write_channel`, several channels in one run, and a mirrored gang write read back holding one tune. **Still unproven on silicon: preset recall as a restore path, all eight channels in one run, and any write while audio is playing.** Renamed from `dsp408_ble.py` on 2026-08-09 once the capture settled the transport; its GATT UUID constants are retained under a fenced "documented dead end" heading, prefixed `BLE_`, so the hypothesis cannot be re-derived from scratch by someone repeating the original scan.

The stack below it, all of which runs against an in-process fake with no hardware:

| Module | Owns |
|---|---|
| `transport.py` | bytes — loopback, capture replay, RFCOMM socket, serial |
| `framing.py` | the byte stream → frames, and the 20-byte padding rule |
| `txpolicy.py` | **what may be transmitted at all** — an allow-list, plus blast-radius caps |
| `session.py` | lock-step transactions, echo matching, pacing, the error contract |
| `device.py` | whole-channel records, read-modify-write, the write journal |
| `snapshot.py` | capture, compare, restore — the rollback mechanism, block-by-block **and** by preset recall |
| `fake_device.py` | an in-process DSP-408 that holds real state and refuses to improvise |

**`ChannelConfig` is a view, not a state.** It cannot express polarity, `spk_type`, mix, dynamics, channel name, link group, or EQ bands 11–30. So `Dsp408Spp` read-modify-writes against the cached 296-byte record and **raises rather than silently ignoring** anything it cannot honour. A backend that quietly drops a field produces a device that does not match the model the optimizer reasoned about, and the improvement invariant then compares a prediction against a system that was never configured as predicted.

**One** thing it refuses outright: **blocks 34/35**, whose meaning is contradicted between the decompiled app and the device's own readback.

> ### ⚠ A whole-channel write must be planned, pre-flighted, then applied
>
> **Encoding can fail on the last frame of a plan.** `Frame.encode()` refuses a
> frame whose checksum computes to zero — the vendor app never sends one — and
> the checksum is a function of the payload *and* `bluetooth_device_id`. So
> whether a block is sendable is not knowable from the parameters alone, and
> some legal tunes contain unsendable blocks.
>
> Until 2026-08-11 `Dsp408Spp.write_channel` encoded and transmitted block by
> block. On real device state it wrote two blocks of OUT1 and then raised on the
> third, leaving the channel **half-configured** — some blocks new, some old, a
> state matching no model anyone reasoned about, on hardware with no undo. The
> improvement invariant would then have compared a prediction against it.
>
> Now `plan_channel()` → `Dsp408Device.preflight()` → apply. Either every block
> is sendable and the write proceeds, or `UnsendablePlan` is raised with nothing
> transmitted. This is the same rule `apply_record` already followed for
> contradicted blocks; the channel writer simply did not.
>
> **The pre-flight builds its frames with `Dsp408Session.block_write_frame`, the
> method the transmitter uses.** A pre-flight that constructs the frame its own
> way is worthless, and the field it will get wrong is `bluetooth_device_id`:
> the first diagnosis of this bug used the default 0 while the session stamps 4,
> found the payload sendable, and confidently answered a question nobody asked.
>
> A refusal is an obstacle, not a hazard — but it is *unfixable from our side*,
> because the same values will fail again. It needs a parameter to move.
> Nudging one LSB of frequency or bandwidth is inaudible and shifts the checksum.

> ### ⚠ A rehearsal against a uniform fake reports passes it did not earn
>
> `DeviceImage.flat()` gives every channel identical settings, no link group and
> no non-flat EQ bands. Against it, the Stage 6 rehearsal wrote **one** block for
> its multi-block test and **skipped the gang test entirely** — and reported
> neither as a problem, because there was nothing to write and nothing to link.
> The bench would have been the first place either path ran, which is the exact
> thing the rehearsal exists to prevent.
>
> `dsp408_probe --fake --fake-from <snapshot>` seeds the fake from a real
> device's records. Under it the same run wrote 7 blocks, exercised the gang, and
> **found the unsendable-frame bug above**. The rehearsal then predicted the
> hardware run exactly: same refusal, same block counts, same gang result.
>
> **A test double's fidelity is part of what the test asserts.** Prefer real
> captured state to synthetic uniformity whenever the code under test branches on
> the state's shape — and treat "the rehearsal found nothing to do" as a failure
> of the rehearsal, not a pass.

**A staged plan is not enforced by being written down.** The bring-up ladder's
first rung is a *no-op* write — a payload byte-identical to what the device
already holds, so the first write in the project's history cannot change
anything even if it lands wrong. That rung was unreachable for months:
`Dsp408Device.write_block` returns without transmitting when the payload
matches, which is right for a restore and made a deliberate no-op impossible,
so the first write to hardware would necessarily have been a real one. Nothing
flagged it, because every layer was correct on its own terms. It surfaced only
when the bench tool was built. **For each stage of a staged plan, name the code
path that reaches it** — a stage with no path is a stage that will be skipped.

The path is `rewrite_block_unchanged`, and it is a **dedicated method rather
than a `force=True` flag** on the general one: it re-reads the block live
immediately before sending it, so the payload cannot differ from the device's
own state by construction rather than by the caller having checked. Same
effect, stronger guarantee. Note what it still cannot show — that the bytes
were *stored*, since they were already there — which is why the first real
write is a separate stage.

**Crossover slope stopped being the other on 2026-08-09.** `h_level`/`l_level` are `slope = 6 × (level + 1)`; `h_filter`/`l_filter` are 0 Linkwitz-Riley / 1 Butterworth / 2 Bessel / 3 Defeat. Fourteen single-control `.DDP` A/Bs, orthogonal in both directions. The bytes read 0, 1 and 3 across all 112 channel-records not because they were dead but because **the corpus had no variation in them** — every saved tune was Linkwitz-Riley at 12 or 24 dB/octave. When a field looks unmappable, check whether the evidence is exhausted before concluding the field is opaque.

**`EqBand.type` went the same way and cut the other direction.** 0 PEQ / 1 low shelf / 2 high shelf, mapped by two A/Bs, and it *added* a refusal: a shelf is not a peaking section and `bw` does not mean what `q_from_bw_raw` says for one, so writing a fitted band into a shelf slot would leave the device running a filter the optimizer never modelled — with a successful write, a matching readback and a plausible fit to hide it. Carrying the field through blind was right while it was unknown and became a hazard once it was known. **Decoding a field can create an obligation, not just an ability.**


> ### ⚠ The channel link mirrors **gain only** — and a gang is not a link
>
> Operator, 2026-08-12, by trying it in the app: with outputs 7 and 8 linked,
> **delay moves independently on each; only gain follows.** By design — the DSP
> is agnostic to *why* two channels were linked, so it mirrors the one control
> a user linking channels almost always means.
>
> This makes the device's `linkgroup_num` narrower than it looks. It is not a
> claim that two outputs are one acoustic thing. It means *mirror the gain
> slider*. **A gang is a fact about a ported box; a link is a fact about a
> slider**, and the existing rule — gang membership is operator knowledge, the
> device flag is only a cross-check — is strengthened rather than weakened.
>
> **Our gang write is deliberately broader than the app's, and should stay
> that way.** Two subwoofers in one enclosure sit at the same place, so any
> delay difference between them is pure comb filtering with no upside. The app
> will let an operator set delay on output 7 and forget output 8;
> `modify_block_mirrored` writes both. That is a divergence from vendor
> behaviour, chosen on purpose, and it is safe because the mutate is narrow:
> each member is read-modify-written individually, so `spk_type`, `polar` and
> every other byte in the block stay that channel's own.




> ### ⚠ Provenance had an acoustic constraint leaking into electrical tests
>
> `Provenance.comparable_to` required a temperature on **every** measurement,
> so an electrical bench comparison -- DSP RCA output straight into a line
> input -- was impossible to satisfy honestly. No propagation path, no room, no
> microphone, and yet no verdict without a thermometer reading.
>
> **The proposed fix was worse than the bug.** Supplying 22 °C to make VERIFY
> go green is cargo-cult metadata: it asserts that a variable is relevant when
> it is not, and records a number about a cable. The operator caught it before
> it was done.
>
> Now `Provenance.coupling` -- `ELECTRICAL` or `ACOUSTIC`, defaulting to the
> stricter acoustic reading so loosening is always a declaration and never an
> omission. Signal-chain terms (device, rate, gains, cal hash) apply to both.
> Temperature participates only for acoustic.
>
> #### And the acoustic case was overstated too
>
> This project has written that "a car's acoustic response moves measurably
> with temperature", with a `temp_tolerance_c = 2.0` default that is
> **uncited** -- exactly the sort of figure the rules forbid asserting from
> memory.
>
> The physics is real but modest: temperature changes the speed of sound by
> about 0.17 %/K, which moves arrival times and therefore the frequencies of
> multipath cancellations. It is **not** a validity cliff at some number of
> degrees. And it is nowhere near the largest term -- **microphone position,
> seat position, doors and windows, HVAC state and occupancy will destroy
> repeatability long before a few degrees does.**
>
> Which exposes the real gap, and it is worse than a loose tolerance: **the
> check gates on the weakest environmental variable and ignores every stronger
> one.** An acoustic comparison can pass it while somebody has moved the
> microphone five centimetres, which at 3.5 kHz is a large fraction of a
> wavelength. That is false confidence, and false confidence is worse than no
> check at all.
>
> The fix is not a tighter tolerance. It is an **operator-declared setup
> token**: a claim that the physical configuration is unchanged, in the same
> family as `DriverCeiling.basis`, `NoIsolation.basis` and `Gang.basis` --
> unverifiable by code, typed by a person, recorded in provenance, compared
> verbatim. Two measurements with different tokens are incomparable whatever
> the thermometer says.

> ### 2026-08-12: the setup token is built, and it moved a check earlier
>
> `Provenance.setup_token`, required for `ACOUSTIC` and optional for
> `ELECTRICAL`, compared literally. `AcousticMeasurer` refuses to construct
> without one. The asymmetry is the design: a token that changes when nothing
> moved costs a refused comparison, and a token that stays when something
> moved is a false verdict, so every default leans toward the first.
>
> Three details that are load-bearing rather than incidental:
>
> - **A token binds an electrical comparison too, once declared** -- it is
>   only *required* for acoustic. A cable moved from OUT1 to OUT2 is a real
>   change that no other provenance field records, and treating a missing
>   declaration as a wildcard would let the weaker of two claims win.
> - **Trimmed at construction, compared verbatim.** Stripping the ends
>   discards nothing. Case folding or collapsing internal whitespace would be
>   leniency at comparison time, and leniency is exactly what lets two
>   different setups match.
> - **It is worth nothing if it is generated rather than typed.** A nonce per
>   session makes everything incomparable (safe, useless); a hardcoded
>   constant makes everything comparable (unsafe). Neither is checkable, the
>   same as `DriverCeiling.basis`, and the honest answer is that this rests on
>   the operator.
>
> #### And the general lesson, which is bigger than the token
>
> The first hardware loop armed, measured, fitted, wrote **eleven blocks**,
> and only then discovered at VERIFY that no thermometer reading had been
> supplied. Nothing was wrong with the tune. The run simply could not say so,
> and *it could have known before it changed anything* -- because the defect
> was structural, not a drift between two measurements.
>
> `Provenance.self_comparable()` is what makes that checkable. Comparing a
> provenance against itself looks vacuous and is not: every pairwise term
> cancels, leaving only what is structurally required. `TuneRun` runs it on
> the **first** measurement set of the run and stops there, before the fit,
> with nothing written.
>
> **A precondition that can be evaluated at the start must not be discovered
> at the end** -- and on a device with no undo, "the end" is after the write.
> Worth checking wherever else the run learns something late.

> ### ✅ 2026-08-12: M4 ran the whole loop on hardware, and accepted
>
> First time `tuner.orchestrate` drove a real backend and a real measurer.
> Electrical bench, OUT1 into the interface, nothing connected to any output.
>
>     [ok] arm        baseline stored to preset slot 6
>     [ok] isolation  declared, bench electrical
>     [ok] floor      0.006 dB from 3 repeats
>     [ok] baseline   1.769 dB
>     [ok] fit        10 bands
>     [ok] write      11 blocks
>     [ok] verify     improved by 0.735, exceeding the floor
>     [ok] settle     accepted; the tune stands
>
> Every stage, on hardware, with the device restored afterwards and verified
> byte-identical. That closes the last layer in the project with no hardware
> evidence.
>
> #### And the known answer is why this was worth doing
>
> The target was **OUT1's own pre-perturbation response**, so the correct
> result was a score near **zero**. It scored **1.034**.
>
> The fit had to reproduce one band: `2514 Hz, −12.00 dB, bw 42`. Instead it
> wrote two straddling notches -- `2368 Hz −7.00 dB` and `2764 Hz −5.30 dB` --
> plus a −9 dB band at 31 Hz outside the scored band and a −2.5 dB gain change.
>
> ~~**The answer sat exactly on `max_cut_db = 12.0`**, the boundary of the
> search space, and differential evolution converges poorly onto a bound.~~
> **That diagnosis was wrong. See the correction below.**
>
> **The improvement invariant was satisfied and correct throughout.** It
> improved by 0.735 dB against a 0.006 dB floor; the run said so accurately,
> recorded everything, and restored cleanly. Nothing misbehaved. The tune is
> simply not very good, and *only a known answer could show that* -- the third
> time today the same lesson has landed. An "it improved" test cannot
> distinguish a good fit from a mediocre one.
>
> Resolved 2026-08-12; the diagnosis above was wrong and the section below
> is the correction.

> ### ⚠ The fitter was not solving the problem it was scored on
>
> **The `max_cut_db` explanation was written from the shape of the result and
> never tested.** Reproducing M4 offline refuted it in one run: on a
> 30-3500 Hz axis the *unmodified* fitter recovered `2514 Hz / -12.00 dB /
> 0.47 oct` to **0.004 dB rms**, with the bound exactly where it always was.
> A boundary that a five-second experiment shows is not binding was never the
> cause, and "differential evolution converges poorly onto a bound" is a true
> statement recruited to explain something it did not explain.
>
> #### The real mechanism
>
> The run's axis was OUT1's passband, **450-3500 Hz** -- three octaves, not
> seven. A -12 dB notch over three octaves moves the curve's own mean by
> **2.46 dB**. `correction_db` level-matches the target by that mean, so the
> correction the fitter was asked for was *the notch, plus 2.46 dB
> everywhere*.
>
> A chain of peaking sections sits at 0 dB between its filters. **It cannot
> make a broadband boost**, `max_boost_db` is 3.0, and boost carries a 4x
> penalty -- so the fitter ate the 2.46 dB as residual across the whole band.
>
> And `MagnitudeObjective` re-level-matches before it scores. **The constant
> was invisible to the verdict and mandatory in the fit.** The fitter was
> spending its entire effort on a term it was not judged on, in the one
> direction its constraints forbid.
>
> #### The fix, and it is one sentence
>
> **Mean-centre the fit's shape term, so the fit optimises what it is scored
> on.** Boost stays uncentred, because boost is absolute: +3 dB costs headroom
> whatever the rest of the curve is doing. Level goes where it belonged all
> along -- `run.py` now reads the gain change off the *fitted* chain rather
> than estimating it beforehand, which is also strictly more accurate.
>
> **The greedy seed had to be centred too, and that is the half that would
> have been missed.** The cost stopped caring about level; the seed did not,
> so it still spent its first bands on a broadband offset. Because the seed
> decides which basin the search lands in, that choice survived to the answer:
> two targets differing *only by a constant* produced fits differing by
> **0.81 dB** in shape. Fixing a cost function and leaving its initialiser
> chasing the old objective is a silent half-fix.
>
> #### Measured, five seeds each, four cases
>
> | case | before | after |
> |---|---|---|
> | **M4's known answer** | 0.814 | **0.008** |
> | known answer + 0.06 dB noise | 0.892 | 0.056 |
> | wiggly channel, flat target, +5 dB hot | 1.920 | 1.033 |
> | same, plus a 14 dB null | 2.000 | 1.904 |
> | broadband tilt | 1.421 | 0.538 |
>
> **Four of five seeds now return exactly one band: `2514.0 Hz, -12.00 dB,
> 0.47 oct`** -- the answer, on the bound, first time.
>
> Two process notes worth more than the numbers:
>
> - **The single-seed comparison lied.** At seed 0 the null case looked like a
>   regression (1.89 -> 2.12) and it was two stochastic fits; over five seeds
>   it is an improvement. This project has already shipped one vacuous test by
>   trusting a single stochastic fit.
> - **The band-recovery assertion was vacuous and the test caught it only
>   because it was checked.** Reverting the fix, the broken fitter *also*
>   placed a band at 2516 Hz / -12.00 dB -- it just surrounded it with nine
>   more. What separates them is the count and where they sit, not whether the
>   right band exists.

> ### ✅ A band fitted outside the measured axis now has to earn far more
>
> Found while fixing the above. On M4's known answer, whose axis is
> 450-3500 Hz, with `freq_range_hz` at its default `(20, 20000)`: the fitter
> returned **nine bands outside the measured axis**, up to `-10.4 dB at
> 20 kHz`, `-8.3 dB at 19 kHz` and `+2.1 dB at 7694 Hz`.
>
> Nothing in the objective could see them -- in band the fit scored 0.009 dB,
> because their skirts nearly cancel. **On a full-range channel every one is a
> real, audible filter fitted from no data**, and the boost is real output the
> stimulus ceiling never accounted for. Hard safety rule 6, arriving by a
> route nobody had considered: not device gain this time, but a filter the
> optimizer invented above the band anyone measured.
>
> #### The root cause: the search cannot decline a band
>
> A `Biquad` has no "off" the optimizer can select, so all `max_bands` filters
> must be placed and the surplus goes wherever costs least. Out-of-band is
> where that is, because the objective has no points there.
>
> Two mechanisms now handle it, and the split matters:
>
> - **`_prune`, backward elimination.** Drop whichever band costs least to
>   lose while that stays under tolerance. A gain threshold cannot do this
>   job: surplus slots routinely land as *pairs* of one- and two-decibel
>   filters that nearly cancel, negligible neither individually nor by gain.
> - **An asymmetric tolerance.** A band outside the axis is priced at
>   `UNMEASURED_PRUNE_TOLERANCE_DB` (0.5 dB) against `PRUNE_TOLERANCE_DB`
>   (0.05 dB) inside it. Nothing constrains such a band at its own centre --
>   only the part of its skirt that reaches the axis -- so keeping it on equal
>   terms applies an evidence standard the data cannot support.
>
> | case, 5 seeds | before | prune | prune, asymmetric |
> |---|---|---|---|
> | known answer: score / bands / outside | 0.814 / 10 / 7.8 | 0.008 / 1.8 / 0.6 | **0.008 / 1.2 / 0.0** |
> | + 0.06 dB noise | 0.892 / 9.8 / 6.2 | 0.056 / 1.8 / 0.8 | **0.055 / 1.0 / 0.0** |
> | wiggly, +5 dB hot | 1.920 / 10 / 0.6 | 1.173 / 9.6 / 0.4 | 1.176 / 9.4 / 0.2 |
>
> **The known answer is now one filter**, and on the noisy variant it is one
> filter at every seed. Where out-of-band bands survive (0.2-0.6 on the
> wide-axis cases) they are earning more than 0.5 dB, which is the standard
> asked of them.
>
> #### ⛔ Refusing the placement outright is worse. Measured twice.
>
> The obvious fix -- clamp band centres to the measured axis -- makes the fit
> **worse**: the known answer goes 0.008 -> 0.077, with 9.6 bands instead of
> 1.2, splitting a single -12 dB notch into `2511/-9.7` and `2619/-8.0` and
> spraying `-8.4 dB at 466 Hz` across the passband for no reason.
>
> Surplus slots that used to escape now land in band, where they **cooperate**
> and pruning cannot dissolve them one at a time. Out-of-band parking was
> acting as a pressure-release valve, and the right response is to charge for
> it, not to close it.
>
> Centring also opened a **flat direction** the clamp exposes: a broadband cut
> is free to a mean-centred residual, since it shifts the mean (which centring
> removes) and is not boost (which the penalty catches). A magnitude penalty
> closes it and is physically right -- a broadband cut has to be given back by
> channel gain, so it costs headroom too -- but its weight was being chosen
> from four synthetic curves, which is how coupled knobs get overfitted. Left
> alone deliberately.
>
> **The clamp was measured once before `_prune` existed and rejected, then
> re-measured with it and rejected again.** The first rejection was not sound:
> pruning is exactly the mechanism that should have absorbed the clamp's
> downside, and concluding without it would have been concluding from a run
> where the thing that fixes the problem was absent. It happened to reach the
> right answer. *A conclusion drawn before the relevant variable exists is
> worth re-running once it does.*


> ### ✅ The floor's timescale, fixed 2026-08-13. ⚠ Its score-dependence, not.
>
> Found 2026-08-12, first time `tuner.orchestrate` drove real hardware. Two
> separate problems with one number, and both cut against the improvement
> invariant. The first is fixed; the second is not.
>
> #### ✅ It was measured over 30 seconds and used to judge a run lasting minutes
>
> `FLOOR` took three sweeps back to back and called their spread the floor. On
> the bench that gave **0.003 dB**. The rollback check then re-measured after
> the whole run — arm, three floor sweeps, baseline, fit, write, verify, roll
> back, several minutes — and found the score had moved **0.006 dB**. Twice the
> floor, so `RollbackFailed`.
>
> Nothing was wrong with the restore; the device was byte-identical. What
> drifted was the rig, slowly, over a span the floor never sampled. **Repeats
> taken back to back measure short-term noise and are blind to drift.**
>
> **It is wrong in both directions at once**, which is why it is not merely
> conservative. Acceptance requires beating the baseline *by more than* the
> floor, so a floor measured too short makes acceptance **too easy**. Rollback
> verification requires matching the baseline *within* the floor, so the same
> number makes unwinding **too strict**.
>
> **The fix costs nothing.** One of the repeats moved to the latest point at
> which the device still holds the baseline — after the fit, which writes
> nothing, and before the write, which is the moment the state stops being the
> baseline. Same sweep count, same bench time, but the repeats now bracket the
> run instead of clustering in its first thirty seconds. `RepeatabilityFloor`
> carries `span_s`, and a verdict whose interval the floor does not cover says
> so in the report.
>
> Three details worth keeping:
>
> - **A floor without a timescale is not a floor**, so `span_s` is a field and
>   not a note. It defaults to 0.0 — covering nothing — because an unstated
>   span should make a caller notice rather than inherit a claim nobody made.
> - **`covers()` asks for half the interval, not all of it, and that fraction
>   is a reporting heuristic rather than a measured quantity.** Equality is
>   unreachable by construction: the verification sweep is the thing being
>   judged, so it is always after the last repeat that can be taken with the
>   device still on the baseline. Requiring full coverage fires on every run
>   by about one sweep, and *a warning that always fires is a warning nobody
>   reads*. A half still catches the case that happened, where the floor
>   covered an eighth of its interval.
> - **It reports, it does not refuse.** Turning a short floor into an
>   indeterminate verdict needs a model of how this rig's noise grows with
>   time, and no such measurement exists. Inventing one is exactly what this
>   file's rules forbid.
>
> `tune_run measure` gained `--spacing-s` for the same reason: a bench floor
> measured in 30 seconds understates what a tuning run will be judged against.
>
> #### The floor's magnitude depends on where the score sits
>
> Subtler, and it cuts both ways:
>
> | Objective's target | Baseline score | Measured floor |
> |---|---|---|
> | the first sweep itself (`tune_run measure`) | ~0 | **0.0585 dB** |
> | a different curve (the loop's reference) | 1.77 | **0.003 dB** |
>
> Same rig, same session, twenty-fold difference. The score is an rms, and rms
> is nonlinear: noise around zero contributes directly, while the same noise
> perturbing a large deterministic deviation barely moves it.
>
> So **a floor is not a property of the rig. It is a property of the rig and of
> how far the baseline sits from the target**, and one measured under one
> objective must not be reused under another.
>
> A run with a distant baseline is therefore simultaneously easier to pass and
> harder to unwind, by the same two-directional mechanism as the timescale
> problem above — and unlike that one, **this half is not fixed.** Nothing
> stops a floor measured under one objective being reused under another: only
> the session id is checked, and two objectives can share a session.

> ### ⚠ ~~No verdict is possible without a temperature reading~~ — half fixed, and the other half is the point
>
> **As written, 2026-08-12:** `Provenance.comparable_to` returned False if
> either measurement had `temperature_c is None`, so a run with no thermometer
> produced `IncomparableProvenance` at VERIFY and was indeterminate every time.
> The first electrical loop run failed exactly there, after fitting and writing
> successfully.
>
> Two things were wrong with that, and they pull in opposite directions.
>
> **It was too strict for the bench.** An electrical measurement has no
> propagation path and no room, so requiring a temperature there asserts the
> relevance of a variable that has none. `Coupling` fixed that the same day.
>
> **It was far too weak for the car**, which is the harder half. Temperature
> was the *only* environmental term, and it is the weakest one there is. The
> answer is not a tighter tolerance but `setup_token`, above.
>
> **And the failure was discovered in the wrong place.** "Indeterminate at
> VERIFY, after fitting and writing" is not a property of temperature; it is
> what happens whenever a run learns a structural fact late. `self_comparable()`
> at the run's first measurement is the fix, and it covers the token, the
> thermometer and anything added later.
>
> What survives unchanged: **an acoustic verdict still needs a real
> temperature**, this rig still has no thermometer, and that is a gap in the
> rig to be closed by measuring, never by relaxing the check.

> ### ✅ 2026-08-12: the preset rollback works on hardware
>
> The last restore path never run on silicon, and the one the closed loop's
> ARM stage depends on. Given its own bring-up rung rather than being
> exercised for the first time inside a tuning run, where a failure would be
> harder to attribute and would abort the run anyway.
>
>     1. store the working area to slot 6   -> working area unchanged
>     2. change OUT1 gain 500 -> 490        -> device differs at output 1
>     3. recall slot 6                      -> gain back at 500, device
>                                              byte-identical to the snapshot
>
> **The perturbation in step 2 is the whole design.** Store-then-recall proves
> nothing, because a recall that did nothing at all would pass it. Changing
> something in between separates the two claims that matter: *a store leaves
> the working area alone* (which is what makes it safe at the start of a run)
> and *a recall really does replace it* (which is what makes it a rollback).
>
> This is the ~5 s whole-device restore the improvement invariant has always
> described, and unlike the block-by-block route it survives our process
> dying, the host going away, and the snapshot file being lost.

> ### ✅ Slots 7-15 echo the last name written — now proven, not inferred
>
> They read `Custom` before the store. After writing the name `tuner-baseline`
> to slot **6**, **all nine of slots 7-15 read `tuner-baseline`**.
>
> The project already believed these slots were a stale buffer, inferred from
> their all returning one identical string. That was consistent with several
> explanations. Writing a *new* name and watching all nine follow is causal:
> they are not storage, they are a view of the most recent name written.
>
> Worth the distinction because it is the difference between "these nine slots
> look unused" and "these nine slots do not exist". `PRESET_SLOT_MAX = 6`
> stands, now on direct evidence.

> ### ⚠ A rollback-by-recall makes the scratch slot the running slot
>
> After recalling slot 6, the device reports **`current_preset = 6`**. It read
> 0 — the live working area — beforehand.
>
> That collides with a rule this project enforces at
> [run.py](../src/tuner/orchestrate/run.py) ARM: *never store the baseline to
> the slot the device is running from*, because that slot is the operator's
> manual fallback. The rule is right and stays. But it means:
>
> **A tuning run that rolls back via preset recall leaves the scratch slot as
> the running slot, and the next run's ARM refuses to start.** Self-inflicted,
> discovered on a bench rung rather than between two in-car tunes, which is
> the whole argument for staging the preset path separately.
>
> The refusal message currently says "choose a different scratch slot", which
> is the wrong advice in this case — the usual fix is for the operator to
> recall their own preset first, which moves the indicator off our slot. Worth
> teaching the message to recognise its own leftovers: if the running slot's
> stored *name* is the one this tool writes, say so and name the real fix.
>
> Note also that the *content* of the working area is unchanged by all of this
> — the recall loaded a slot we had just stored from it. Only the indicator
> moved. An operator looking at the app will nonetheless see preset 6 active
> where it previously showed none.

> ### ✅ Settled 2026-08-12 by measurement: **ten EQ bands, not thirty**
>
> The enthusiast community has held for years that the DSP-408 has 30 usable
> PEQ bands per channel. **It does not.** Only the first ten execute.
>
> | Slot | Written | Measured | Predicted | Verdict |
> |---|---|---|---|---|
> | **1** | −6 dB @ 1 kHz | **−6.04 dB** | −6.00 dB | **live** — 0.062 dB rms over 300 points |
> | **11** | −6 dB @ 1 kHz | −0.22 / −0.43 dB, and −0.007 dB by absolute sweep | −6.00 dB | **inert**, three independent runs |
> | **31** | −6 dB @ 1001 Hz | −0.25 dB | −6.00 dB | **inert** |
>
> Firmware `MYDW-AV1.06`, differential sweeps on the bench against a session
> repeatability floor of 0.0585 dB. Slot 1 is the control and it lands on the
> model; slots 11 and 31 do nothing at all.
>
> **No compaction.** Slot index maps straight to hardware biquad index. Slot 11
> did nothing despite nine unused slots below it, so the firmware is not
> packing non-flat bands into ten hardware sections.
>
> #### Why the belief is reasonable, and why it is dangerous
>
> The channel record has **31 EQ slots**. All of them accept writes. All of them
> read back byte-exact. `DataID` 0..30 are addressable and the `.DDP` file
> stores every one. Anyone probing the protocol — which is how this project
> found them too — sees thirty-one bands and draws the obvious conclusion.
>
> **The failure is completely silent.** Write a band to slot 11: the write
> succeeds, the `0x51` ack comes back, the whole-record readback matches, the
> `.DDP` shows the band. Nothing at the protocol layer distinguishes it from a
> working write. Only a measurement can.
>
> That is worth stating plainly to anyone building on this protocol: **on this
> device, a verified write is not a working write.** Everything this project
> does to validate a write — ack, readback, whole-record comparison, journal —
> passes on a band that has no effect whatsoever.
>
> The record is almost certainly sized for a larger sibling product, which is
> the same explanation as block 34 being called `MIX_IN_9_16` on a four-input
> device and block 33 carrying eight input slots for four inputs. Three
> independent oversizings, one shared codebase.
>
> #### Consequences here
>
> `max_peq_per_channel = 10` is now **measured acoustically**, not merely
> observed in the vendor UI — a stronger evidence grade for the same number.
> `ADDRESSABLE_BANDS` stays 31 because that is a true fact about the record
> layout, and keeping the two separate is what made this testable at all.
> Collapsing them was on the task list until the question was raised.

> ### ⚠ A metric that reported a finding the device never produced
>
> Two runs of `predict-check --band 10` reported a **+1.888 dB level shift**,
> reproducing across runs to **0.004 dB**. Reproducibility that tight reads as
> a real effect, and it was written up as "writing slot 11 perturbs the signal
> chain" before being checked.
>
> It was the metric. `predict-check` printed `mean(measured − predicted)` under
> the label **"level drift"**, which is only what that quantity means when the
> prediction is flat. With a −6 dB notch predicted and nothing measured, it
> returns `−mean(predicted)` — and `mean(predicted)` over that band is
> **−1.887 dB**. The reported figures were +1.884 and +1.888.
>
> Three things caught it, and all three are worth keeping:
>
> - **A control on a known-good band, run between the suspect runs.** Slot 1
>   came back at 0.062 dB rms with zero offset, which said the rig was fine and
>   pointed the suspicion at the interpretation rather than the hardware.
> - **An absolute measurement when the differential looked strange.** Sweep,
>   write, sweep, roll back, sweep. `B − A` was −0.007 dB mean, 0.106 rms —
>   nothing. A differential metric cannot see a common-mode error, and a
>   *mislabelled* differential metric cannot see its own mislabelling.
> - **Arithmetic on the claim.** `mean(predicted)` took one line to compute and
>   matched the "measurement" to three decimals. A finding that equals a
>   quantity already in the program is not a finding.
>
> The tool now prints `mean measured` and `mean predicted` separately, labels
> the residual as a residual, and measures level drift **outside the filter**,
> where it means something.
>
> The general form: **a derived statistic reproducing precisely is evidence
> that the derivation is deterministic, not that the effect is real.** Both
> runs recomputed the same wrong thing from the same inputs.

> ### 🔬 ~~Planned~~ ANSWERED: are EQ bands 11-30 live, or only stored?
>
> **The community has claimed for years that the DSP-408 has 30 usable PEQ
> bands per channel.** The vendor app exposes ten. Both can be true: the
> 296-byte channel record has **31 EQ slots** and the device reads and writes
> all of them, so the question is not whether they are *addressable* — they
> demonstrably are — but whether the firmware's signal chain *runs* them.
>
> This project listed it as "no reason to want them" on 2026-08-12 and that
> was wrong. Thirty bands is three times the correction resource. Whether a
> fitter *should* spend that many is a separate argument — more bands at one
> microphone position is a good way to fit the microphone position — but the
> resource question comes first, and it is cheap to settle.
>
> **The experiment is `predict-check` pointed at a high band.** Everything
> needed already exists:
>
> 1. On the bench, outputs disconnected, write a known band to slot **11**
>    (`--band 10`) via `tune_run predict-check`.
> 2. Differential sweep: band flat, band set, take the difference.
> 3. Compare against `biquad.response_db`.
>
> The outcomes are unambiguous and there is no third one:
>
> | Measurement | Conclusion |
> |---|---|
> | The notch appears, matching prediction | Slots 11-30 are **live**. `max_peq_per_channel` rises to 30 and the community is right |
> | Flat, within the repeatability floor | Slots 11-30 are **stored but not executed**. Ten is real, and we can say so with a measurement instead of a UI observation |
>
> The rig resolves 0.065 dB against a 0.0585 dB floor, so a −6 dB band either
> appears or it does not; there is no ambiguous middle. Ladder it — 11, 20, 30
> — because the boundary might not be where the UI implies.
>
> **Why this is safe to run and safe to defer.** The slots are inside the
> addressable range, they read flat today, and a write to one is rolled back
> from a snapshot like every other bench write. Deferring costs nothing
> because `max_peq_per_channel = 10` already caps the fitter; the extra slots
> are flattened, never fitted.
>
> **This is why `ADDRESSABLE_BANDS` (31) and `max_peq_per_channel` (10) must
> stay separate.** The first is a fact about the record layout and is not in
> doubt; the second is a claim about what the firmware executes and is exactly
> what the experiment tests. Collapsing them to one number — which was on this
> project's task list until the question was raised — would have hard-coded
> the answer before measuring it.

> ### ✅ The bandwidth domain is `bw_raw` 0..295, and the endpoints re-prove the formula
>
> Operator, 2026-08-12, reading the app's own limits: **Q ranges 0.404 to
> 28.852.** Those are not round numbers, and that is what makes them evidence.
>
> | operator's Q | `bw_raw` | octaves |
> |---|---|---|
> | **28.852** | **0** | 0.05 |
> | **0.404** | **295** | **3.000** |
>
> Both match `q_from_bw_raw` to three decimal places, and the low end lands on
> exactly **3.000 octaves**. So the stored bandwidth is an integer in
> **`bw_raw` ∈ [0, 295]**, spanning 0.05 to 3.00 octaves in 0.01-octave steps —
> a clean range with clean endpoints, which is what a real parameter looks like.
>
> **This is a third independent confirmation of `octaves = (raw + 5)/100`.** The
> `+5` offset is what puts the minimum at 0.05 rather than 0, and it is the part
> of the formula that could most easily have been wrong without any measurement
> noticing: a wrong offset shifts every bandwidth by a constant, which a fitter
> absorbs into neighbouring bands and a listener never hears. The three routes
> now agreeing are the app's displayed Q, the measured half-gain widths at
> `bw_raw` 25/65/134, and these two endpoints.
>
> **Our `FitConstraints` are narrower than the device, on purpose.** `min_q=0.5,
> max_q=8.0` sits inside 0.404–28.852 and should stay there — a Q of 28 is a
> notch about a twentieth of an octave wide, which is position-sensitive enough
> that it usually corrects a measurement artefact rather than a real feature,
> and which rings. **What has to change is the labelling**: those numbers are
> currently indistinguishable from unmeasured placeholders, and they are a
> deliberate engineering choice with the device's real limit now known beside
> them.

> ### ⚠ Shelves exist, on two apps out of three, on two bands out of ten
>
> Operator, 2026-08-12, after being pointed at bands 1 and 10 specifically:
>
> | | band 1 | band 10 | bands 2–9 |
> |---|---|---|---|
> | **Windows / Android** | *(presumed PEQ / LS — see below)* | **PEQ or HS** | PEQ only |
> | **iOS** | PEQ only | PEQ only | PEQ only |
>
> The corpus agrees and fills the gap: three `.DDP` files carry a **low shelf on
> band 1 at 31 Hz** and a **high shelf on band 10 at 16 kHz**, and `type != 0`
> appears nowhere else in 9 920 bands. Band 1 offering PEQ/LS is inference from
> those files rather than an observation of the UI, and is flagged as such.
>
> Three things follow.
>
> **The shelf refusal in `_plan_peq` is live, not theoretical.** An operator who
> sets band 10 to HS in the Windows app makes that channel unfittable until it
> is set back — the backend refuses rather than writing peaking parameters into
> a shelf. That is the right behaviour and it is now a thing that can actually
> happen to this car.
>
> **iOS is not a superset, and this is the second confirmed divergence.** The
> first was gain being displayed as `−10.0 dB` on Android and `0–60` on iOS.
> Now iOS is missing a feature the other two have. **"The vendor app" is not a
> single thing**, and any claim sourced to a UI names which one — a rule this
> project already learned once and has now been handed a second reason for.
>
> **The first answer was wrong in a useful way.** Round 1 reported "no shelf
> options, just the 10 PEQs". The corpus contradicted it, and the contradiction
> pointed at exactly two bands out of ten in two apps out of three. A confirmation
> would have closed the question; the contradiction found the shape of it.

> ### ✅ Block 33 is the mixer, decoded 2026-08-12
>
> **8 bytes, one per input, 0-100 — the same numbers the app's mixer grid
> shows.** Only the first four are meaningful on a 4-input device; bytes 5-8
> are zero in all 40 `.DDP` files and on the live device.
>
> Live routing, read straight out of block 33:
>
> | Output | IN1 | IN2 | IN3 | IN4 |
> |---|---|---|---|---|
> | 1, 3, 5 | 80 | — | 80 | — |
> | 2, 4, 6 | — | 80 | — | 80 |
> | 7, 8 | 90 | 90 | 90 | 90 |
>
> **Confirmed against an independent claim.** `docs/hardware.md` says outputs
> 1/3/5/7/8 are reachable from input 1 and 2/4/6 are not — derived on the bench
> by sweeping and listening for silence, months before this block was decoded.
> The two agree exactly. Two routes, no shared reasoning.
>
> That also explains **why the subwoofers take all four inputs at 90**: they
> are summing the whole car, which is what a mono sub feed looks like.
>
> #### And it kills the `DataType 3` lead
>
> The iOS app showing "input values 0-100" was recorded on 2026-08-11 as the
> first evidence of a vendor path to the unmapped input section. **It was the
> mixer.** Both apps have it, both show 0-100, and the values live in the
> *output* channel record at block 33 — nothing to do with `DataType 3`, which
> remains unreached by any vendor app on any platform.
>
> A lead that survived a day because nobody had looked at the screen. One
> screenshot retired it.


> ### ⚠ Blocks 34/35 are almost certainly vestigial — still refused
>
> Evidence as of 2026-08-12, and it changes the *reason* for the refusal
> without changing the refusal.
>
> Both blocks read `a4 01 38 00 f4 01 00 00` on **every channel of the live
> device**, and the same constant appears throughout the corpus. As uint16 LE
> that is 420 / 56 / 500 — the `OutputDynamics(all_pass_q, attack, release)`
> the decompiled app describes. **The operator confirms there is no dynamics,
> compressor, limiter or all-pass control anywhere in either app.**
>
> Block 35 differs from 34 in exactly one byte: the last, which is
> `linkgroup_num` — 1 on outputs 7 and 8, 0 elsewhere. That byte is live and
> everything around it is not.
>
> The decompiled app calls block 34 `MIX_IN_9_16`. Block 33 turned out to hold
> **eight** input levels on a device with four inputs. Both facts point the
> same way: this is a **shared codebase for a larger sibling product**, and on
> a 4-in/8-out DSP-408 the upper half of the mixer and the dynamics section
> are simply not wired to anything.
>
> So the earlier framing — "contradicted between the decompiled app and the
> device's readback" — was probably wrong in an interesting way. The app is not
> contradicting the device; it is describing a product this is not.
>
> **The refusal stands, for a better reason.** There is nothing to gain by
> writing a field the firmware ignores, and block 35's last byte is a live link
> group that a careless whole-block write would clobber. Circumstantial, not
> proven: constant across 40 files and 8 channels, plus an absent UI, is strong
> evidence and not a measurement.

> ### ⚠ A gang can be a mechanical constraint, and the device's flag is not enough
>
> **Outputs 7 and 8 drive two subwoofers in one ported box** (operator,
> 2026-08-10). They are gain-matched because the box pressure is common to both
> cones: drive them unequally and one takes more than its share of the
> excursion while the other is back-driven, which below port tuning is how a
> driver fails. So they are **one acoustic source to measure** and **one
> correction to write** — gain, delay and EQ alike.
>
> All 40 `.DDP` backups have the two outputs identical in gain, low-pass and
> delay. **Fourteen of them store `linkgroup_num` as 0.** Those are the files
> from the session where the app wrote unlinking and never wrote re-linking, so
> the pair stayed matched by the operator's intent while the device's own flag
> said otherwise.
>
> `link_partners()` reads that flag from the device, which is correct and stays
> correct — the app is less reliable, not more. But during those sessions it
> would have returned empty for 7 and 8, `modify_block_mirrored()` would have
> written one subwoofer and not the other, and `refuse_linked_channels` would
> not have fired, because the device says there is no link to refuse.
>
> **Every guard we have for that pair is keyed off a flag that reads zero
> exactly when it matters.** The rule this generalises to: *a device flag is
> evidence; for a constraint whose violation breaks hardware, evidence is not
> enough.* Gang membership is operator knowledge, declared in the plan with a
> basis, and the device's flag becomes a cross-check that warns loudly when it
> disagrees rather than a source that silently overrides.
>
> **Built 2026-08-10 as `orchestrate.plan.Gang`.** A gang is one *source*:
> swept once, fitted once, written to every member, and then **read back** to
> confirm the members hold one tune. Readback rather than a comparison of what
> we sent — a partial write, a refused frame or an off-by-one channel id all
> produce the mismatch, and none is visible from the sending side. The check
> runs at ARM too, and a gang already mismatched *before* the run stops it
> rather than being silently levelled: levelling changes something nobody
> asked us to change, and a rollback would restore the mismatch anyway.
>
> Two words that had been one: an **output** is a DSP channel, a **source** is
> one thing measured once and corrected once. The delay pool is charged per
> output, the objective is weighted per source, and a two-driver gang is one
> of the latter and two of the former.

**Linked channels are no longer one of them.** Measured 2026-08-09: the device mirrors nothing — a write changes exactly the channel addressed — and the vendor app keeps a linked pair consistent by sending two writes ~10 ms apart, each a full read-modify-write of that channel's own block. So writing one half of a pair is safe for the *device* and wrong for the *model*: the optimizer reasoned about a pair. Use `Dsp408Device.modify_block_mirrored()`. And **read the link group from the device, never from the app** — in two separate captures the app wrote unlinking and never wrote re-linking, so it can display a pair as linked while `linkgroup_num` is stored as 0.

**`OutputMisc.enabled` is 1 for on and 0 for muted** — the opposite sense to the name the decompiled app gives that byte. Settled 2026-08-09 by an A/B in the vendor app that changed exactly one byte in the whole backup, with `gain_raw` untouched, so muting is a real separate control rather than a gain zeroing. The field was carried as `byte0` in `ddp.py` for months rather than adopting the APK's name, on the strength of it reading 1 on 111 of 112 channel-records; declining to guess was right, and the guess it declined to make turned out to be correct. What that bought was that nothing was written on the strength of it.

> ### ⚠ The transport decision was wrong, and is now measured
>
> This section previously read "**The transport is settled: Bluetooth Low Energy**", on the strength of a GATT scan that found a writable `0xFFE2` characteristic, plus a classic-Bluetooth SDP scan that found only A2DP and AVRCP.
>
> **An HCI capture of the vendor Android app driving the device shows classic Bluetooth RFCOMM — that is, SPP — and no BLE at all.** 2918 protocol frames crossed dynamically-allocated L2CAP channels carrying RFCOMM UIH frames with credit-based flow control. Exactly five packets in the whole capture touched the BLE ATT channel, and none of them carried our protocol.
>
> A scan that finds a writable characteristic tells you what the device *offers*, not what the vendor software *uses*. The SDP scan that found no SPP was looking at the wrong thing or was incomplete; the capture is direct evidence of the app using it.
>
> **M3 is an RFCOMM socket, not a GATT client.** That is a serial-port abstraction on every platform we care about — a COM port on Windows, `rfcomm`/`socket(AF_BLUETOOTH, SOCK_STREAM, BTPROTO_RFCOMM)` on Linux. `bleak` is not the dependency; nothing needs it.
>
> Whether the BLE path *also* works is now an open question rather than the plan. See `docs/dsp408-protocol.md`.

**RFCOMM is a byte stream, and frames split across it.** The observed payload is 20 bytes while any parameter write is at least 24, so **every write is fragmented**. A reader that scans packets individually finds the short read frames and silently misses every write — which is precisely what ours did until the stream was reassembled. Treat the transport as a stream and frame it by preamble, length and checksum.

**Frame encoding lives in `tuner/dsp/protocol.py`**, which is pure logic with no I/O and is fully tested offline. Keep it that way: the transport module should do nothing but move bytes. See `docs/dsp408-protocol.md` for the wire format and its derivation.

**Do not let protocol or ADAU1701 details leak above the backend boundary.** The optimizer talks in engineering units; the backend owns the translation.

> ### ⚠ Four device-protocol hazards
>
> 1. **Destructive opcodes sit among ordinary parameter blocks.** `DataType 9` with `ChannelID` 96/97/98 are `RESET_MCU`, `TRANSMITTAL` and `RESET_GROUP_DATA`; 95 and 99 are normal. `Frame.encode()` refuses them unless `allow_destructive=True`. Nothing about a ChannelID's value signals danger, which is why the protocol was decoded before any bytes were sent.
> 2. **Frequency is continuous; bandwidth is not.** Measured 2026-08-08: a 450 Hz crossover corner reads 449.4 Hz, where snapping to the nearest table entry would have given 420 Hz. `EQ_FREQ_TABLE_HZ`/`XOVER_FREQ_TABLE_HZ` are the app's default band layout, **not** device constraints, and `nearest_eq_index`/`nearest_xover_index` are not quantizers. Fit frequency continuously. **Bandwidth is genuinely quantized** — an integer `bw_raw` at 0.01 octave per step — so fit in the bandwidth domain, never in Q: one raw step is worth 0.60 in Q near Q=9.6 and 0.002 near Q=0.5.
> 3. **A preset recall is a *read*.** Measured 2026-08-09: `user_id` on an
>    `OUTPUT_CHANNEL` frame selects a preset slot (0 = the live working area,
>    1–6 = storage), and the vendor app recalls a preset by *reading* `DataID` 0
>    on channels 0–7 with it set. There is no select opcode and no write in the
>    sequence. So a frame that looks in every respect like an ordinary read
>    overwrites the entire working tune, all eight channels, with no undo — and
>    `txpolicy` permitted it, because reads were assumed safe. **Reads are not
>    inherently safe on this device.** Related: `DataID` 0 is overloaded, 8 bytes
>    being EQ band 0 and 296 bytes a whole channel record, with only the length
>    telling them apart.
>
>    The same mechanism is the rollback the improvement invariant needs: store
>    the baseline to a slot, and one recall restores all eight channels in ~5 s.
>
>    **Never store to the slot the device is currently running from.** That one
>    is the operator's manual fallback — recalling it is how a person restores
>    the car when nothing we wrote is working. A run that overwrites it still
>    looks correct, because it has two restore paths of its own, and one digit's
>    typo in a plan is all it takes. `tuner.orchestrate` refuses it, before the
>    store rather than after.
> 4. **The device has no undo, and every write is immediately permanent.** Measured 2026-08-08: parameter writes go straight to non-volatile storage and survive a power cycle with no commit step. The vendor app's bypass/restore is *app session memory*, not a device feature — unplug USB and the pre-bypass values are gone. A backend we write has no equivalent, so **`.DDP` backups and preset slots are the only rollback that exists.** Treat that as a correctness requirement of the improvement invariant, not a convenience. Recalling a preset overwrites the working area but does not modify the preset itself, which makes a preset slot the one restore point that survives everything.

## Ask the operator before reverse-engineering

The operator has physical access to the hardware, the vendor tooling, the
vehicle, and memory of how the system was built. **None of that is visible in
the repository, and it is routinely cheaper, faster and more reliable than
deriving the same fact from measurement or decompilation.**

This is not a courtesy. It has already redirected the project twice:

- **`dspcartunebackups.DDP` sat unexamined in the repository root** while a
  differential acoustic experiment was being planned for the delay question.
  The file answered that question outright, and later contradicted the
  assumption that PEQ and crossover frequencies are quantized to fixed tables —
  a premise the optimizer was about to be built on.
- **The input gain knob was documented as "eyeballed near minimum"** for a whole
  session, marking every absolute level in it as provisional. A ten-second
  physical check established it was hard against the stop, which retro-validated
  those figures instead of requiring them to be re-measured.

Both were cheap facts sitting one question away from a plan that did not need
making.

**Before committing to any discovery path — a bench experiment, a decompile, a
protocol spike, a purchase — inventory what the operator already has.** The
categories that have paid out or plausibly will:

| Category | Examples |
|---|---|
| Vendor exports | Saved tunes, presets, factory defaults, backups from other units |
| Vendor UI state | What the app *displays* next to a raw value we hold — a screenshot maps encodings for free. **Ask which app**: iOS and Android render the same parameter differently, and iOS exposes input gain that Android does not |
| Spare or donor hardware | A second unit converts "too risky to write" into "run the experiment" |
| Physical access | Opening the case, probing continuity, photographing a board settles questions no amount of black-box measurement can |
| System knowledge | What is actually wired to each output, its power handling and crossover — this sets the safety ceilings |
| Prior measurements | Earlier REW captures, a shop's tuning session, before/after files |
| Test equipment | Meter, scope, SWD probe, logic analyzer — often already on the bench |
| Manuals and datasheets | For the *installed* parts, not the generic ones |

How to ask:

1. **Ask only for things that could shorten or falsify the current plan**, and
   say what each answer would change. Generic background questions waste the
   operator's attention and train them to skim.
2. **Ask before the plan is written, not after.** The point is to avoid building
   a plan around a gap that was never real.
3. **Operator answers are evidence and get recorded with provenance.** A file is
   stronger than a photograph, which is stronger than a remembered setting.
   Record which one it was. "The operator recalls setting 500 Hz" and "the saved
   file says 450 Hz" are not the same claim and must not be written down as
   though they were.
4. **An operator observation that contradicts the repository wins** until a
   measurement says otherwise, and the document gets corrected the same session.
5. **Never ask the operator to do something the safety rules forbid us to do.**
   Physical access does not suspend the hard safety rules; it usually means the
   operator can verify a routing assumption we would otherwise have to trust.


### Check the vendor UI *first*, as a standing step

The section above says to inventory what the operator has before committing to
a discovery path. In practice that kept happening **incidentally** — the useful
question got asked halfway through building the thing it made unnecessary. It
has closed a roadblock roughly six times in a week, every time by accident of
timing.

So it is now a step with a place in the order of work rather than a virtue:

1. **Before planning any measurement, decompile, capture or bench experiment,
   list the assumptions it rests on.** Not the goal — the assumptions.
2. **Mark each one "visible in the vendor UI?"** The taxonomy below is what has
   actually paid out; use it as a prompt, not a limit.
3. **Batch every UI-visible one into a single set of questions and ask them
   before the plan is written.** Batching matters: a question per hour trains
   the operator to skim, and the value is in the ones nobody thought to ask.
4. **Record answers with their evidence grade.** A saved file beats a
   screenshot beats a recollection, and the three are not interchangeable.
5. **When an answer dissolves a planned experiment, say so explicitly** and
   retire the experiment in the same edit. Otherwise the ledger keeps carrying
   work that no longer needs doing.

**What the UI has actually settled**, and therefore what to look for:

| Category | Worked examples |
|---|---|
| **Maximums and ranges** | Delay 8 ms/channel; 10 PEQ bands, not the 31 addressed. Closed a question costed at three bench-session-sized routes |
| **Whether a control is ganged or independent** | The link mirrors gain but not delay — a model correction nobody would have thought to ask for |
| **Which controls exist at all** | iOS exposes input gain; Android does not. First evidence of a vendor path to `DataType 3` |
| **Units and display encoding** | Output gain reads `−10.0 dB` on Android and `0–60` on iOS. Two apps, one parameter, and "confirmed against the app display" was ambiguous for months |
| **What is currently set** | Cheaper to read than to infer, and the basis for every measurement's interpretation |
| **Whether an operation is offered at all** | Preset store/recall; bypass; whether a field is editable or only displayed |

**The cost asymmetry is the whole argument.** A UI check is about a minute and
carries no risk to the only unit. The alternatives — a bench session, a
decompile, a logic-analyser capture, a binary search against hardware with no
undo — cost hours to days and sometimes put the device at risk. The reason the
question gets skipped is never that it is expensive; it is that by the time the
plan exists, asking feels like a detour. **Hence step 1 sits before planning,
not inside it.**

The live list lives in **[docs/ui-question-register.md](docs/ui-question-register.md)**: open questions with what each unblocks, the ones the UI cannot settle and their real cost, and the closed ones with what they replaced. That last column is the argument for the practice.

**Device state is cheap to verify, so verify it rather than inferring it.** The
operator can read, change and confirm any channel's configuration in minutes.
Any measurement whose interpretation depends on what the device is set to —
which is most of them — should start from a confirmed configuration, not a
remembered one. Until parameter scaling is quantified well enough to spot-check
a curve against the settings that produced it, **doubt about device state is
resolved by asking, not by reasoning from the last thing we were told.**

Prefer a saved backup to a verbal confirmation: `.DDP` files are parsed by
`tuner.dsp.ddp`, diffable with `tools/ddp_dump.py`, and are the strongest
evidence grade available. That loop has already caught a change to two channels
that nobody had noticed making.

## Absorbing an external review

Challenges to this project's claims — an adversarial review, a second opinion, a
reviewer's counter-pass — get **checked against the code before anything is
edited**, and the result recorded as a scorecard: *claim, verdict, evidence*,
one row each, with file and line.

This is not ceremony. The first such review, on 2026-08-09, made fifteen
falsifiable claims. Ten were right and materially improved the project. **Three
were wrong**, and each would have caused a real regression if absorbed on
authority:

- "The timing-reference rule is unenforced" — it is enforced and tested.
- "Provenance refusal is unenforced" — likewise.
- "`require_linear_path` always returns indeterminate on filtered channels" —
  real bench data passes.

Deferring to a confident reviewer would have produced three wrong doc edits and
possibly three wrong code changes. Checking cost minutes.

The rules:

1. **Verify before conceding.** A claim about the code is settled by the code.
2. **Record the refutations too**, with evidence, in the same table as the
   confirmations. A scorecard listing only what you accepted is a changelog, and
   the next reviewer re-raises what was already answered.
3. **Concede narrowly and mark the boundary.** When a claim is wrong in its
   stated mechanism but right in spirit, say exactly that. The block 34/35 case
   is the worked example: "RMW loses undecoded bytes" was false, but RMW through
   blocks whose *meaning* is contradicted is genuinely dangerous, so the
   "Refuted" row carries a callout saying it must not be read as "safe to write".
4. **Look for what the review missed.** The 2026-08-09 review did not spot the
   unguarded `group_delay_samples` or the untested `IncomparableProvenance`
   branch. A review is a prompt to audit, not a complete audit.
5. **Sequencing defects count as findings.** The counter-pass on the resulting
   plan found no design errors but three ordering errors, one of which would have
   destroyed the working tune. Order is part of correctness on a device with no
   undo.

6. **Collect on the evidence a session produced, not just describe what it
   did.** Round 4's two best findings were both failures to update the
   open-questions table after a session answered part of it — the ledger
   recorded a success while still predicting that success might fail. So: **for
   every open question whose "when it bites" moment just happened, write down
   what the session showed, including "still open, and here is why what we got
   does not settle it."**

   This is a sharper form of Round 3's "re-read what you touched", which was
   tried and was not enough: the sweep it prescribed caught every mechanical
   defect and no semantic one. A grep-shaped sweep finds grep-shaped errors.

The worked examples live in `docs/review-2026-08-09.md` — four rounds, with the
refutations recorded beside the confirmations. Across them, **35 claims: 25
confirmed, 8 refuted, 2 partial.** A reviewer being right two times in three is
the reason to check all three.

## Validation policy

This matters because every downstream stage inherits the measurement engine's errors. A deconvolution bug does not announce itself; it produces a smooth, plausible curve that is simply wrong. So what each layer is actually checked against, and how strong that check is, has to be stated exactly.

| Layer | Validated against | Strength |
|---|---|---|
| Wire protocol | **Real device traffic.** All 5834 frames in `captures/btsnoop_hci.log` re-encode byte-identically; `tests/golden/dsp408_frames.json` pins a 49-frame subset. Two further captures add 61 742 frames covering presets, mute, mixer, master volume, link mirroring and the disconnect | **Independent.** Ground truth from outside this project |
| Bulk channel record | **The vendor app's own read-from-device.** Our RFCOMM readback equals the output section of three `.DDP` backups, 2368 bytes, byte for byte (`tests/test_bulk_record.py`) | **Independent.** Two paths sharing no code |
| Measurement engine | **REW.** Both tools measured the same DSP output electrically; over 30 Hz–3.5 kHz they agree to **0.35 dB max, 0.09 dB rms, 0.01 dB of tilt** across 375 points, through a 12 dB notch and the shoulder of a 24 dB/oct low-pass. Plus analytic known-answer tests and a ±0.35 dB electrical loopback | **Independent** below 3.5 kHz — a second implementation, sharing no code, answering the same physical question. **Nothing above 3.5 kHz is validated**, because the reference's own run-to-run scatter (0.370 dB rms) exceeds the disagreement there. Our engine repeats to 0.080 dB |
| Parameter scaling | Measurement: a requested 6.00 dB step moved the passband 6.01 dB; a 450 Hz corner measured 449.4 Hz; EQ `level` raw 720 produced +11.98 dB; EQ `bw` raw 25/65/134 produced their requested half-gain widths to ±0.8 % | **Independent** for all four. Closed 2026-08-09 — nothing rests on the vendor app's display any more |
| **The whole write path, end to end** | **Measurement, through our own backend.** A `Biquad` the fitter could have chosen — 1 kHz, −6 dB, Q 2 — translated by `_band_to_eq`, encoded by `protocol`, written over RFCOMM, executed by the ADAU, measured differentially and compared against `biquad.response_db`: **rms 0.065 dB, max 0.298 dB over 300 points, 450–3500 Hz**, against a session repeatability floor of **0.0585 dB** | **Independent, and at the noise floor.** The model and the device cannot be told apart on this rig |
| Microphone calibration | Recalibrating a mic that has a factory cal file should reproduce it within the stacked error budget (~±1.5–2 dB) | Planned; `tuner.cal` is not built |

> ### ✅ Paid 2026-08-09: the REW goldens exist
>
> This section carried a debt notice. It had earlier carried something worse — a claim that the engine "is validated against REW", with reference data in `tests/golden/`, when **no such test had been written and no such data existed**. The debt notice replaced the claim; this replaces the notice.
>
> `tests/test_golden_rew.py` compares our frequency response against REW 5.31.3 measuring the same DSP-408 output electrically, one after the other with nothing touched between. Over the device's **30 Hz–3.5 kHz** passband, across a 12 dB notch and the shoulder of a 24 dB/octave low-pass: **max 0.416 dB, rms 0.094 dB, 0.014 dB of tilt, 375 points.** The tolerance — 0.5 dB max, 0.25 dB rms — was fixed in the file before the data was taken and has not moved.
>
> Regenerate with `tools/bench_golden.py`. A reference you cannot reproduce is a fixture.
>
> #### The band stops at 3.5 kHz because the reference does, not because we do
>
> **Nothing above 3.5 kHz is validated**, and it is worth being exact about why, because the first two explanations were both wrong.
>
> The band was chosen by a rule stated in advance — *derive it from our own run-to-run repeatability, which needs no reference, then compare.* Under WASAPI our runs agree to 0.392 dB out to **8 kHz**, so the rule gave 8 kHz, and at 8 kHz the comparison **failed** — up to 1.9 dB just above the low-pass corner, while our own runs there agreed to 0.004 dB. A neat hypothesis followed: REW windows the impulse, group delay peaks at a corner, REW read lower. Two experiments killed it.
>
> **Remove the low-pass.** On a channel flat to 18 kHz at full level, the disagreement did not vanish — it grew to 2.1 dB. Never about the slope.
>
> **Measure the reference against itself.** Two REW runs, identical settings, unchanged path:
>
> | Comparison | max | rms |
> |---|---|---|
> | our two runs | 0.395 dB | **0.080 dB** |
> | **REW's two runs** | 3.311 dB | **0.370 dB** |
> | ours vs REW run 1 | 2.110 dB | 0.261 dB |
> | ours vs the mean of both REW runs | 1.987 dB | **0.201 dB** |
>
> **REW's own scatter exceeds our disagreement with it, and averaging its two runs moves it toward us** — what noise does, and what a systematic error does not. Nothing is left to explain: the engines agree as closely as the reference can resolve. **Our engine is 4.6× more repeatable than the tool checking it.**
>
> It cannot be improved on this rig. REW averages sweeps safely only with a timing reference, and for *this* comparison the Solo's two inputs were both spoken for — the DUT on one and a mic preamp on the other, leaving no spare channel for a loopback.
>
> ### ⚠ That sentence was read far too widely, 2026-08-13
>
> "No spare channel for a loopback" is true of the **electrical bench**
> comparison it was written about, where input 1 carries the DSP's output.
> It was then carried forward as though it were a property of the interface,
> and it is not.
>
> **The Scarlett Solo has two simultaneous inputs** — a 1/4" line/instrument
> input and an XLR input with 48 V — and in an *acoustic* measurement only one
> of them is needed for the microphone. So:
>
>     output L --> DSP RCA in        (stimulus)
>     output R --> input 1, LINE     (hardware loopback)
>     mic      --> input 2, XLR+48V
>
> **One clock, absolute delay, `TimingReference.LOOPBACK`** — on hardware we
> already own. The blocker was never the interface. It is that the UMIK-1 is a
> USB microphone and therefore cannot use the interface's preamp at all, which
> is what forced the split clock and the whole acoustic-reference apparatus.
> The purchase that fixes it is **a microphone, not an interface.**
>
> Two things to check when it is wired: the loopback cable is a new ground
> path between an output and an input, which is exactly the class of fault
> that once raised this rig's noise floor 43 dB, so `_verify_quiet` earns its
> keep; and the front input must be switched to **line**, not instrument.
>
> #### ⚠ And a warning this project had only ever verified on its own rig
>
> Asked to average 8 sweeps *without* a timing reference, REW produced **62 dB** of comb filtering: no error at DC, growing with frequency because phase error is proportional to `f × Δt`, then oscillating. `_combine_passes` aligns passes to sub-sample precision before combining for exactly this reason, and its docstring's "24 dB of span on a loopback that is flat to a third of a dB" was our own measurement. Now it is reproduced independently, and kept as `tests/golden/rew/flat_rew_8sweeps_unaligned.txt`.
>
> **More sweeps is not automatically better.** Averaging without alignment is worse than not averaging, and the result looks like a catastrophic system response rather than an averaging bug.
>
> #### ⚠ The unvalidated band is where the fragile drivers are
>
> Independent validation stops at 3.5 kHz. **Tweeters live above it**, crossed
> at 3.5 kHz on this very system, and that is also where EQ errors are most
> audible and where rule 4's most fragile drivers sit.
>
> Nothing about that is unsafe on its own — the safety limiter does not depend
> on measurement accuracy, and above 3.5 kHz our engine is *more* repeatable
> than the reference (0.080 dB rms against 0.370). But confidence in this
> project is **band-dependent**, and a tune's high end rests on a weaker
> validation claim than its midrange. Say so when reporting one.
>
> #### Use WASAPI, not MME. Measured, not preferred.
>
> Same DUT, same level, back-to-back. Run-to-run scatter, dB max:
>
> | Band | MME | **WASAPI** | WDM-KS |
> |---|---|---|---|
> | 250–1000 Hz | 0.515 | **0.231** | 0.267 |
> | 3500–5000 | 0.317 | **0.091** | 0.251 |
> | 5000–7000 | 1.772 | **0.384** | 0.352 |
> | 7000–10000 | 1.418 | **0.792** | 1.504 |
>
> MME is 2–5× worse from 250 Hz to 10 kHz — 4.6× at 5–7 kHz, where the signal is a comfortable −33 dBFS, so this is not a signal-to-noise story. **A large part of the project's long-standing "HF artifact above 4 kHz" was MME.** WDM-KS bypasses the Windows mixer and ran 30 dB quieter, so its numbers are not comparable; it was not pursued.
>
> **The switch is licensed by a known-answer check**: WASAPI against MME agrees to 0.236 dB max, 0.058 dB rms. A host API that changed the *answer* would invalidate every earlier measurement; one that changes only the *scatter* leaves them comparable, well inside the 0.39 dB repeatability floor.
>
> Above 10 kHz nothing helps, but this DUT has nothing there to measure. Earlier flat-channel runs with real HF content repeated to ~0.4 dB at 14.5 kHz under MME, so **no claim is made about >10 kHz with signal present.**

The general rule this is an instance of: **a validation claim names its reference and its independence, or it is not a validation claim.** "Tested" against a fixture we generated is a regression test — useful, and not the same thing.

The calibration rig (`tuner.cal`) doubles as a second known-answer test: calibrating a microphone that already has a factory calibration file should reproduce that file within the stacked error budget (~±1.5–2 dB).

## What not to do

- **Don't add learned components to the core tuning path.** Deterministic core, always.
- **Don't reach for an accelerator.** Compute is not the constraint in this project. **Measured 2026-08-08**, now that the fitter exists: a 10-band fit over a 300-point log axis takes ~5.4 s per channel at the default iteration cap, so ~43 s for eight channels, plus seconds of FFT for the impulse responses. The constraints are audio I/O reliability and microphone calibration. Optimizing compute is solving a problem that does not exist here.

  Two things did matter, and both were algorithmic rather than hardware: evaluating biquad responses directly instead of per-band `freqz` calls (2.5×), and seeding differential evolution with a greedy pass instead of a random population (6×, *and* a better fit). The iteration cap is set where further search buys less than the measurement repeatability floor — past that point the optimizer is fitting noise.
- **Don't put microphone preamps on an SBC HAT.** In a 12 V automotive environment, sensitive preamps sharing rails with a busy SoC means alternator whine and ground loops. Audio hardware stays external and class-compliant over USB; that boundary also keeps the host commoditized.
- **Don't assume more microphones improve accuracy.** With a valid timing reference, sequential single-mic measurement is phase-correct. Additional mics buy *unattended iteration speed* — which is decisive for an automated tuner, but it is a different argument and should not be confused with accuracy.
- **Don't state ADAU1701 or DSP-408 figures from memory.** Cite the datasheet or the spike findings. **This extends to published curves.** `optimize.target.harman_in_car` raises rather than reproducing values, because a wrong target is inherited by every tune afterwards and no measurement can reveal it — the tune will faithfully match whatever curve it was handed. Supply real numbers through `from_points()` with the source in the curve's name.
- **Don't measure a filter's shape with one sweep when two will do.** A single sweep forces the fit to absorb the speaker, room, microphone and interface into one offset term, which only works if all of them are flat. A reference sweep with the band flat, divided out, cancels every one of them exactly — verified to 1 part in 10⁴ through a deliberately hostile synthetic system. `tools/bench_peq.py --differential`.
- **Don't cross-correlate one driver against another to find their time offset.** It is the wrong instrument and fails hardest on the pair that matters: a tweeter crossed at 3.5 kHz and a mid-woofer rolled off at 450 Hz share essentially no passband, so the correlation peak is built from stopband leakage and can land an octave's worth of samples away while looking confident. Compare each driver's arrival against the *loopback* instead — that is what `arrival_samples` is.

## Development setup

Host-agnostic by design: desktop first, embedded target later. The audio layer (`tuner/audio`) abstracts over PortAudio via `sounddevice`, so the same code runs on Windows, Linux and macOS without modification.

```bash
pip install -e ".[dev]"
pytest
```

Tests must pass with no audio hardware attached and no DSP connected.
