# Hardware

## Host platform

**Decision: N100-class x86 mini-PC (~$150).**

Compute is not the constraint in this project. The full per-round workload — roughly 32 impulse responses of FFT plus differential-evolution biquad fitting across 8 channels — is seconds of FFT and under a minute of optimization on modest hardware. Choosing a host on compute grounds is solving a problem that does not exist here.

What actually drives the choice is **audio I/O reliability**, and secondarily the Windows tooling needed for the M0 protocol spike.

| Candidate | Verdict |
|---|---|
| **N100 x86 mini-PC** | **Chosen.** Genuinely small (~12 cm square). Best-tested USB audio stack of any option. ~4× the floating-point of a Pi 5. Runs Windows for USBPcap and SigmaStudio, which M0 requires — collapsing two boxes into one. |
| Pi 5 + Audio Injector Octo HAT | Viable fallback if a Pi is wanted. 6 analog in / 8 out over native I²S via a CS42448 — no USB audio stack in the path at all. Six inputs is exactly 4 mics + loopback + spare. Line-level, so it needs an external mic front end. Note the convergence: the interface design settled independently on the same CS42448, which makes the Octo a useful open-hardware layout reference regardless of host. |
| Pi 5 + USB interface | Workable but risky; USB multichannel capture on ARM is the weak link. |
| D-Robotics RDK X3 | **Rejected.** A53-class floating point, vendor kernel, unverified USB Audio Class 2 support, and a 5 TOPS BPU that is an int8 CNN accelerator for vision — it cannot accelerate deconvolution or biquad fitting. |

### Why not an SBC audio HAT with onboard preamps

In a 12 V automotive environment, microphone preamps sharing power rails with a busy SoC means alternator whine and ground loops. An external interface has its own regulation and is quieter.

The boundary also keeps the host commoditized: once the audio hardware is a class-compliant USB peripheral, it moves between machines and the host stops mattering. That is the correct place to draw the line.

## Audio interface — designed, not built

**No interface exists yet.** The earlier recommendation of a Behringer UMC1820 is **withdrawn**: it is a 19" 1U rackmount unit, which does not fit a working-in-a-car workflow. Form factor was missing from the original criteria and is now a hard requirement.

The decision since taken is to **build one**. Full design, component shortlist, staging and acceptance criteria: **[measurement-interface.md](measurement-interface.md)**. Summary of the requirements it is designed against:

| Requirement | Why |
|---|---|
| **Compact / portable** | Must be workable inside a vehicle, not on a rack rail. |
| **6 inputs** | 4 mics + internal loopback + spare. |
| **4 outputs** | Drives all four DSP-408 analog inputs. |
| **Electret bias, no phantom** | Measurement electrets need a few volts, not 48. The phantom supply is one of the main reasons commercial interfaces are big and costly. |
| **Fixed calibrated gain — no knobs** | A gain knob makes `Provenance.gains_db` a remembered number rather than a measured one. See the design doc. |
| **One clock domain** | Inter-channel phase correctness by construction. |
| Line inputs flat to 20 Hz | The current rig's ceiling — see the measured Scarlett results below. |
| Class-compliant USB | Keeps the host commoditized. |
| **USB bus-powered** | The host already has an automotive DC-DC; avoids a second supply in the car. |

Buying instead was considered and rejected on the two points in bold that no commercial interface offers: every one of them has gain controls, and none can provide an internal loopback. Compact desktop interfaces (MOTU M6 class) and battery **field recorders** (Zoom F6/F8) remain the fallback if the build stalls — the field-recorder category was overlooked first time round and fits the form-factor and power constraints better than any desktop box.

> **Whatever is used, verify early:** a simultaneous multichannel capture test is a Milestone 1 **acceptance gate**, not an optional diagnostic. A partially-working interface is worse than a non-working one, because it produces data that looks fine.

## Microphones

**On hand:** miniDSP UMIK-1 (USB, factory calibrated), Focusrite Scarlett Solo.

**Target array:** 4× PUI AOM-5024L-HD-R electret capsules in custom bodies with in-capsule balanced drivers, calibrated in-house against the UMIK-1 by the substitution method (see `docs/measurement-theory.md` and `tuner.cal`). Buy 8 — spares are ~$4 and allow best-four selection after calibration. **Their 110 dB max SPL carries a headroom trip-wire that must be measured in-vehicle** before the array is trusted; see [measurement-interface.md](measurement-interface.md).

This replaces the earlier plan of 4× Behringer ECM8000 (~$140). The ECM8000 needs 48 V phantom; dropping phantom power is what lets the interface be small and bus-powered, so the mic and interface decisions are one decision. Build details in [measurement-interface.md](measurement-interface.md).

The UMIK-1 stays **out** of the array permanently and serves as the reference standard. This is the correct role for it: as a USB device it has its own clock and cannot be sample-synchronous with interface inputs. Four of them would not be an array — see the design doc for why this is not a matter of degree.

### Why DIY calibration is adequate here

Stacked error from substitution calibration lands around ±1.5–2 dB absolute — worse than a factory-calibrated EMM-6, vastly better than an uncalibrated mic. But absolute accuracy is not what an array is for. Mics calibrated against the same reference by the same procedure share their common-mode error, so **relative** mismatch between array elements stays small, and relative matching is what spatial averaging and L/R asymmetry analysis depend on.

Cost comparison for four mics: roughly $150 in capsules, drivers, bodies and connectors, DIY-calibrated, versus ~$360 for 4× Dayton EMM-6 factory-calibrated — and the EMM-6 route would additionally require phantom power, which is what forces the interface to be large. The rig also doubles as a known-answer test of the measurement engine, which is the better argument for building it.

### How many microphones

With a valid loopback timing reference, **sequential single-mic measurement is phase-correct** — it is how REW does time alignment. Extra mics do not buy accuracy.

What they buy is *unattended iteration*. With one mic, a human repositions it between every sweep, making a five-round optimization loop a multi-hour job. With four mounted once at ear height, eight outputs is ~2 minutes per round and the whole measure→fit→apply→re-measure loop runs untouched. For an automated tuner that is the difference between the concept working and not.

Beyond four, returns fall off sharply: one DSP cannot satisfy eight conflicting seat optima, so additional positions mostly average away the detail they cost.

## Interim setup (before the interface arrives)

The UMIK-1 has no loopback path, so it cannot provide a hardware timing reference. Milestone 1 therefore covers magnitude response, RT60 and spatial averaging — roughly 80% of the measurement engine — with time alignment deferred. The measurement API enforces this: phase and delay raise rather than returning a plausible wrong number.

The Scarlett Solo has a spare instrument input that can be wired as a hardware loopback, but only one mic preamp and no XLR mic to put in it yet.

## Measured: Scarlett Solo interim rig (2026-08-06)

Characterized by electrical loopback — rear Line Output L → front input 2 via a
1/4" TRS cable, INST off, nothing else connected. Measured with this project's
own engine, which is what validated the engine end to end.

### Results

| Property | Measured |
|---|---|
| Flatness, 100 Hz – 15 kHz | **−0.29 .. +0.36 dB** (median of 3 runs) |
| Level linearity, −40 to −6 dBFS | **0.25 dB** spread |
| Pre-arrival noise floor | −106 dB below peak |
| Round-trip latency | ~10 700–12 400 samples @ 44.1 kHz (MME) |
| Single-run dropout artifacts | up to ~3 dB, random frequencies |

Conditions: 44.1 kHz, −6 dBFS, input 2 gain at its minimum hard stop, Windows
audio enhancements **off**.

The interface is essentially flat. Earlier reports in this document of a
low-frequency rolloff were wrong — see below.

### Findings that change how measurements are run

**Windows voice processing silently destroyed four separate measurements.** The
capture endpoint enumerates as "Microphone", so Windows applied speech
noise-suppression to it: an aggressive downward expander whose gain varied by
**80 dB** with input level (−102 dB at −40 dBFS vs −24 dB at −6 dBFS at 1 kHz),
opening earliest at 3 kHz and latest at 300 Hz. It manufactured an apparent
70 dB/octave low-frequency "cliff" that does not exist, and separately caused
low-level tones to vanish into digital silence. Disable it: Settings → System →
Sound → Input → device → **Audio enhancements → Off**.

**Always test level-linearity before trusting a frequency response.** Play tones
at several output levels and confirm gain is constant; it takes ten seconds. A
linear path shows identical gain at every level. Non-linear processing anywhere
in the chain — Windows enhancements here, but equally a head unit's loudness
compensation or an amplifier's limiter in a car — produces a smooth, plausible,
completely wrong curve that a single-level measurement cannot detect.

**MME drops samples; use the median of at least 3 runs.** Individual captures
show narrow-band artifacts up to ~3 dB, and the affected frequencies *move
between runs* (8.3–8.8 kHz, then 2.0–3.4 kHz, then elsewhere). Median across
repeats removes them: three runs spanning 2.7, 1.2 and 4.0 dB individually gave
a median curve spanning 0.65 dB. Never characterize anything from a single
sweep.

**Latency is not repeatable, which is why the timing-reference rule exists.**
Round-trip latency varied by 30 samples between back-to-back runs at identical
settings, and by ~1 600 samples across sessions. Any stored latency constant
would be wrong on the next run. This is direct empirical justification for
requiring a hardware loopback rather than calibrating latency once.

> **Superseded 2026-08-08.** The 30-sample figure was measured too loosely.
> With the DSP in the path it is **307 samples run-to-run and 574 samples
> between passes within one capture**. See the DSP-408 bench section below.

**44.1 kHz is native.** Confirmed in Windows Sound settings ("2 channels,
24 bit, 44100 Hz") and by measurement. Requesting 48 kHz makes Windows resample,
costing ~3 dB at 10–16 kHz. Our rate is independent of the DSP-408's fixed
48 kHz because its I/O is analog, so this costs nothing.

### Host API notes

Only **MME** works on this machine. WDM-KS fails to open at all (Focusrite's
driver exposes no kernel-streaming pins), WASAPI exclusive opens output-only at
44.1 kHz and no input, and this PortAudio build has no ASIO host API. MME's
~250 ms latency is irrelevant given the loopback reference; its dropouts are
not, hence the median-of-N rule above.

Windows endpoint volumes matter: the capture endpoint was found at 0.537 scalar
and had to be forced to 1.0. Check both endpoints before trusting a level.

## Measured: DSP-408 on the bench (2026-08-08)

First session with the DSP in the signal chain. Rig: Scarlett rear Line Output
L → DSP-408 RCA input 1 → DSP RCA output 1 → Scarlett front input 2. No
speakers; entirely electrical. Interface confirmed as **Scarlett Solo 3rd Gen**
(USB `VID_1235 / PID_8211`).

### Scarlett Solo 3rd Gen, from the manufacturer user guide

Cited rather than remembered, because gain staging depends on them.

| | |
|---|---|
| Line input, max level | **+22 dBu at minimum gain** |
| Line input impedance | 60 kΩ; gain range 56 dB |
| Line input THD+N | <0.002 % (minimum gain, −1 dBFS in) |
| Line output, max level | **+15.5 dBu at 0 dBFS**, balanced |
| Line output impedance | 430 Ω |

Minimum gain is the condition Focusrite *specifies* the interface at, which is
a second reason to sit there beyond repeatability.

**AIR is analogue on the 3rd Gen and applies to the mic preamp only.**
Focusrite Control duplicates two front-panel switches (AIR, INST) and contains
no DSP. It is not needed, and nothing in it can affect the line-in → line-out
path.

### Results

| Property | Measured |
|---|---|
| DSP flat baseline, 100 Hz – 10 kHz | **0.4 dB span** (±0.22 dB, 32 Hz – 6.3 kHz) |
| Level linearity, −40 to −20 dBFS | **0.07 dB** spread |
| Session repeatability floor, 100 Hz – 10 kHz | **0.387 dB worst / 0.213 dB mean** (5 runs) |
| Idle noise floor, clean rig | −71 dBFS rms |
| Round-trip latency drift, run to run | **307 samples** (5 runs) |
| Round-trip latency drift, pass to pass | **574 samples** (7 passes, one capture) |

### Findings that change how measurements are run

**Latency jitter is ~20× worse than previously recorded.** This document
formerly said 30 samples between back-to-back runs. Measured properly: **307
samples between runs and 574 samples between passes inside a single capture**,
at 44.1 kHz over MME. The timing-reference rule is better founded than the old
figure suggested. It also kills any plan to measure delay by comparing arrival
times across two separate captures — 1 cm of DSP delay is ~1.41 samples, so the
jitter swamps the quantity by more than two orders of magnitude.

**Alignment leaves substantial residual phase.** After `_combine_passes`
aligns, passes still disagree by 15.9° at 1 kHz, 62.1° at 8 kHz and **97° at
16 kHz**. The implied timing error is not constant across frequency (1.95 /
0.95 / 0.74 samples), so it is not a simple residual delay. Separately, the
real/imaginary median in `_combine_passes` is not phase-coherent and costs a
consistent few tenths of a dB above 4 kHz against a magnitude median. Neither
explains the occasional multi-dB narrowband HF outlier, which remains open.

**A USB control cable raised the noise floor by 43 dB.** Connecting the
vendor app over USB-B injected a **100 Hz harmonic series** — not mains, which
is 60 Hz here. Idle rms went from −70.9 to −27.4 dBFS; the 100 Hz fundamental
from −111.7 to −26.9 dBFS. It is generated inside the DSP (unchanged with the
DSP's analog input disconnected) and powered by the supplied Dayton wall wart.

*Configure over USB, then unplug it before measuring.* This is also an
independent argument for the BLE transport: BLE is galvanically isolated, so
there is no ground path to loop.

**Host audio reaches the measurement.** A video playing on the host's default
output — the same interface — raised the idle floor from −71.3 to −29.9 dBFS
(voice core −82.3 → −25.2 dBFS) and moved a measured crossover corner from
493 Hz to 470 Hz, a −6 % error, **while still producing a smooth,
plausible-looking Linkwitz-Riley curve**. Swept-sine deconvolution is
impressively robust — in-band SNR was ~6 dB and the result only moved ~1.5 dB —
but robust is not immune.

**MME device indices are not stable.** MME lists the host's default output
first, so its numbering shifts when the default changes: the Scarlett moved
from output index **3 to 7** when the Realtek was made default. A hard-coded
`device=(1, 3)` then silently addressed the PC speakers while still capturing
the correct input, producing a smooth curve made entirely of noise.

> **Select audio devices by name, host-API qualified** — e.g.
> `'Speakers (Scarlett Solo USB), Windows WASAPI'` — never by index. Name
> lookup fails loudly when ambiguous; index lookup fails silently with the wrong
> hardware.

> **Prefer WASAPI to MME.** Measured 2026-08-09 on this rig, same DUT and level,
> back to back: MME's run-to-run scatter is **2-5x worse from 250 Hz to 10 kHz**
> — 1.772 dB against 0.384 at 5-7 kHz, where the signal is a comfortable
> -33 dBFS, so this is not a signal-to-noise story. A large part of the
> long-standing "HF artifact above 4 kHz" was MME.
>
> The switch is licensed by a known-answer check: WASAPI against MME agrees to
> **0.236 dB max, 0.058 dB rms**, so it changes the scatter and not the answer,
> and historical MME measurements stay comparable inside the 0.39 dB
> repeatability floor. WDM-KS bypasses the Windows mixer and ran 30 dB quieter;
> not comparable, not pursued.

Provenance previously recorded only the input device, so this was invisible in
the record. It now records both directions.

### Gain staging and the knobs

The **monitor knob is at its maximum hard stop**. An analog knob has exactly
two reproducible positions — the two end stops — and level is controlled
digitally in dBFS, which is recorded in provenance.

The **input gain knob is at its minimum hard stop** — operator-verified by
physical inspection, 2026-08-08. This document previously recorded it as an
*eyeballed* position near minimum ("7 o'clock"); that assertion was wrong, and
the knob had not been moved between then and the check, so the absolute dBFS
figures recorded here were taken at minimum gain and stand as written. Minimum
gain is also the condition Focusrite specifies the interface at, so the cited
+22 dBu max input level and THD+N figures apply directly.

Both knobs are therefore at end stops, which is the only reproducible state an
analog control has. Level is set digitally in dBFS and recorded in provenance.
Any future session that finds either knob off its stop invalidates comparison
of absolute levels against this section — check both before trusting a
cross-session delta.

### Bench topology

The DSP is removed from the vehicle and brought indoors for testing. On the
bench it is entirely electrical — **no speakers, no amplifier, nothing on any
output but the one under test**:

```
Scarlett rear Line Output L  ->  DSP RCA input 1
DSP RCA output N             ->  Scarlett front input 2
```

Only one output is measured at a time, and the cable is moved to select it.
This matters when choosing which channel to measure, because **the tune does not
route input 1 to every output.** Outputs 2, 4 and 6 are fed from input 2 and
would measure as silence with this wiring — a capture that `require_signal_response`
would correctly reject, but which would otherwise look like a dead channel.

| Reachable from input 1 | Not reachable |
|---|---|
| OUT1, OUT3, OUT5, OUT7, OUT8 | OUT2, OUT4, OUT6 |

OUT7 and OUT8 are additionally at `gain_raw` 0, so they are expected to be
silent even though they are routed.

### Vehicle channel assignment

Operator-supplied, 2026-08-08. Corroborated by the crossover corners in the
saved tune, which match the driver types exactly.

| Output | Driver | Tune's passband |
|---|---|---|
| 1, 2 | Midrange | 450 – 2500 Hz |
| 3, 4 | Tweeter | 2500 – 20000 Hz |
| 5, 6 | Mid-woofer | 55 – 450 Hz |
| 7, 8 | Subwoofer | 20 – 55 Hz |

**This does not license raising any ceiling.** Knowing a channel is a tweeter is
the reason its ceiling stays at the conservative default, not a reason to move
it. Raising one needs the driver's power handling and sensitivity, which are
still unknown; `ChannelLimit.characterized` stays `False` on all eight until
then. Outputs 5–8 are the only ones where a raise is likely to be justifiable
later, and only with driver data in hand.

The assignment is irrelevant to bench work — nothing is connected — but it is
what the safety configuration must encode before the unit goes back in the car.

## Loopback wiring

One interface output wired directly back to one interface input, at line level with appropriate attenuation. This establishes t=0 for every capture.

Reserve the loopback channel in configuration and never route stimulus to it acoustically. Its presence is recorded per-measurement; see `tuner.measure.result.Measurement.has_timing_reference`.

## Bill of materials

All figures approximate and unverified.

| Item | Approx. cost | Notes |
|---|---|---|
| N100 mini-PC | $150 | Host |
| 6-in/4-out interface build | $255 | Custom, self-assembled with reflow + stencil. Includes parts for a second control board. Itemized in [measurement-interface.md](measurement-interface.md) |
| XK-AUDIO-316-MC-AB dev board | $245 | Stage-2 de-risking; one-off, not part of the final rig |
| 4× electret measurement mics (8 capsules) | $151 | PUI AOM-5024L-HD-R, balanced drivers, bodies, TA4F, cable. Calibrated in-house |
| Sealed coupler build | $30 | Box, driver, grommets — see `tuner.cal.coupler` |
| Stands, clamps | ~$50 | Ear-height mounting in-vehicle |
| **Total** | **~$730 first unit** | ~$485 excluding the one-off dev board. Excludes the already-owned UMIK-1, Scarlett Solo, and reflow/paste tooling. Budget a rev-B PCB + stencil (~$55) as likely |

In-vehicle power: the mini-PC needs an external supply. The interface is USB bus-powered from the host, so it needs nothing of its own — one of the reasons for that choice. A 12 V DC-DC for the host is the only vehicle-side power item.
