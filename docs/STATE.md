# Project state

**Updated: 2026-08-13, end of session.** The closed loop ran on hardware on
2026-08-12; everything since has been preparing the acoustic path, which has
**still never been run**. Read this first after a context reset. `CLAUDE.md`
holds the rules; this holds the situation.

**The project is now public: https://github.com/nick0451/dsp408-tuner**
(MIT). Note for anyone resuming: the repository used to be rooted at the
operator's *home directory* with zero commits, so a `git add -A` would have
published credentials and personal files. That is fixed and the stray `.git`
is gone; do not recreate one above the project.

## Where the project stands

**Every layer now has hardware evidence.** M4 was the last one without any, and
it ran on 2026-08-12: arm, isolation, floor, baseline, fit, write, verify,
settle, accepted, device restored and verified byte-identical afterwards.

    pytest 1508 passing    ruff clean
    dsp408_probe rehearse 29/29    tune_run rehearse 41/41

**Since then, two fixes, both below.**

**The fit.** M4's known answer -- one exactly-representable band the run had to
reproduce -- scored 1.034 where ~0 was correct. The published explanation for
that (`max_cut_db`, a search-space bound) was **wrong, and reproducing it
offline refuted it in one run**. The real cause was the fit's cost demanding an
absolute level match that a chain of peaking filters cannot make. Mean-centring
the shape term takes the reproduction from **0.814 to 0.008 dB rms**.

The same investigation found the fitter placing most of its bands **outside
the frequency range that was measured** -- nine of ten, up to `-10.4 dB at
20 kHz` and `+2.1 dB at 7694 Hz` -- where nothing constrains them and every
one is a real filter once written. Root cause: the search cannot decline a
band. Pruning, with a tolerance five times stricter outside the measured axis
than inside it, closes it: **the known answer is now one filter, and none of
them sit outside.** Refusing the placement outright is measurably worse.

**The setup token.** Provenance now requires the operator's verbatim claim
about the physical configuration before it will compare two acoustic
measurements, because gating on temperature alone was gating on the weakest
environmental variable and ignoring every stronger one. It also moved the check
that catches this from VERIFY to the run's first measurement, so a run that
could never reach a verdict stops before it fits or writes anything.

**The floor's timescale.** Three sweeps back to back measured thirty seconds of
noise and were then used to judge a comparison spanning minutes -- which makes
acceptance too easy *and* rollback verification too strict, from one number,
and which reported `RollbackFailed` on a device that was byte-identical. One
repeat now moves to the latest point at which the device still holds the
baseline, after the fit and before the write. Same sweep count, same bench
time, and the repeats bracket the run.

**The acoustic timing reference.** A USB microphone cannot provide a hardware
loopback, and this project concluded from that it was magnitude-only forever.
Too broad: a reference speaker playing a short chirp either side of the sweep
gives a common acoustic t=0 -- so a subwoofer's arrival becomes measurable
because the *reference* carries the timing -- and the interval between the two
chirps measures the two clocks' ratio over exactly the window that matters.
`TimingReference` is now NONE / LOOPBACK / ACOUSTIC, and the middle state
refuses absolute delay while permitting relative. Built and known-answer
tested; not yet wired into `capture_sweep`.

### The one-paragraph version

The measurement engine is validated against REW below 3.5 kHz and is 4.6x more
repeatable than the reference above it. The DSP-408's control protocol is
decoded and validated byte-for-byte against 61 742 captured frames, and the
whole control stack -- framing, transmit allow-list, transports, in-process
fake, lock-step sessions, read-modify-write, snapshot/restore, preset
store/recall -- is proven on the real device through every bring-up stage. The
model of the device matches the device to 0.065 dB rms, at the measurement
floor. The closed loop runs. The fitter's known-answer failure was diagnosed
and fixed on 2026-08-13 -- 0.814 dB rms to **0.008**, ten bands to one, on the
filter M4 could not reproduce -- after the first, untested explanation for it
turned out to be wrong.

### What is proven on hardware

| | |
|---|---|
| Reads | 31/31, firmware `MYDW-AV1.06`, all eight 296-byte records, zero resyncs |
| Idle survival | 120 s of silence, link intact, no keepalive needed. A lower bound |
| Write transport | Fragmented 24 -> 2x20 chunks, acked `0x51` under our pacing |
| Write effect | `gain_raw` 500 -> 490, readback confirmed, neighbouring bytes preserved |
| Scale | 46 writes across 4 channels, whole-channel `write_channel`, gang write read back holding one tune |
| Rollback, block-by-block | Restores and re-reads. Proven repeatedly |
| Rollback, preset recall | Store, perturb, recall, byte-identical. ~5 s for all eight channels |
| Model vs device | 0.065 dB rms against a 0.0585 dB floor. Indistinguishable |
| **The closed loop** | **Ran, accepted, restored** |

### What is still unproven on silicon

- **All eight channels in one run.** The most a run has touched is four.
- **A write while audio is playing.** Every write so far went into silence.
- **Anything acoustic.** No microphone has ever been in the loop;
  `AcousticMeasurer` reached hardware for the first time on 2026-08-12 but
  through a cable, not a room.

### ✅ The fit was the weak link. Fixed 2026-08-12 -- and the first diagnosis was wrong

The loop's target was **OUT1's own pre-perturbation response**, so the correct
score was ~0. It scored **1.034** against a baseline of 1.769, having written
two straddling notches where one exactly-representable band was the answer.

**The first explanation -- that `-12.00 dB` sat on `max_cut_db` and
differential evolution converges badly onto a bound -- was written from the
shape of the result and never tested. Reproducing it offline refuted it in one
run**: on a 30-3500 Hz axis the unmodified fitter recovered the band to
0.004 dB rms with the bound untouched.

The real cause: the run's axis was OUT1's passband, **450-3500 Hz**. A -12 dB
notch across three octaves moves the curve's own mean by **2.46 dB**, and the
level split matches by that mean -- so the fitter was asked for *the notch,
plus 2.46 dB everywhere*. A peaking chain cannot make a broadband boost,
`max_boost_db` is 3.0, and boost carries a 4x penalty. Meanwhile
`MagnitudeObjective` re-level-matches before scoring, so **that constant was
invisible to the verdict and mandatory in the fit**.

The fix is one sentence: **mean-centre the fit's shape term so the fit
optimises what it is scored on**, boost excepted because boost is absolute.
The greedy seed needed centring too -- it still chased the level the cost had
stopped charging for, and since the seed picks the basin, two targets
differing only by a constant produced fits 0.81 dB apart.

Five seeds, four cases:

| case | before | after |
|---|---|---|
| **the known answer** | 0.814 | **0.008** |
| known answer + 0.06 dB noise | 0.892 | 0.056 |
| wiggly channel, +5 dB hot | 1.920 | 1.033 |
| same, plus a 14 dB null | 2.000 | 1.904 |
| broadband tilt | 1.421 | 0.538 |

Four of five seeds now return **exactly one band: `2514.0 Hz, -12.00 dB,
0.47 oct`**.

**The improvement invariant behaved correctly throughout the original run** --
improved by 0.735 dB against a 0.006 dB floor, reported accurately, restored
cleanly. Necessary and not sufficient, demonstrated on hardware rather than
argued. Nothing about that changes; the tune it accepted was simply mediocre,
and only a known answer could show it.

#### ✅ And bands placed where nothing was measured

The same investigation found the fitter returning **nine bands outside the
measured axis** -- to `-10.4 dB at 20 kHz` and `+2.1 dB at 7694 Hz` on a
450-3500 Hz measurement. Invisible to the objective, real filters on the
device, and the boost is output the stimulus ceiling never accounted for.

Root cause: **the search cannot decline a band.** A `Biquad` has no "off", so
all `max_bands` filters get placed and the surplus goes where it costs least,
which is where the objective has no points.

Two mechanisms, and the split matters. `_prune` runs backward elimination --
drop whichever band costs least to lose while that stays under tolerance --
because a gain threshold cannot catch surplus slots that arrive as *pairs* of
one- and two-decibel filters which nearly cancel. And the tolerance is
**asymmetric**: 0.05 dB in band, 0.5 dB outside it, since nothing constrains a
band at a centre frequency nobody measured.

| case, 5 seeds | before | prune | prune, asymmetric |
|---|---|---|---|
| known answer: score / bands / outside | 0.814 / 10 / 7.8 | 0.008 / 1.8 / 0.6 | **0.008 / 1.2 / 0.0** |
| + 0.06 dB noise | 0.892 / 9.8 / 6.2 | 0.056 / 1.8 / 0.8 | **0.055 / 1.0 / 0.0** |
| wiggly, +5 dB hot | 1.920 / 10 / 0.6 | 1.173 / 9.6 / 0.4 | 1.176 / 9.4 / 0.2 |

**The known answer is now one filter.**

**Clamping placement to the axis is worse, measured twice** -- 0.008 -> 0.077,
9.6 bands instead of 1.2, the single notch split in two. Surplus that used to
escape lands in band, where it cooperates and pruning cannot dissolve it.
Out-of-band parking was a pressure-release valve; charge for it rather than
closing it. A residual flat direction remains -- a broadband cut is free to a
mean-centred residual -- and is deliberately left alone: the penalty that
closes it is physically right, but its weight was being chosen from four
synthetic curves.

### The target deployment changed, 2026-08-13

The tuner is heading for a **Raspberry Pi** carried to the car: UMIK-1 and a
USB audio interface on USB, RFCOMM to the DSP, and a local web app the
operator drives from a phone. Decisions and the porting checklist are in
[pi-deployment.md](pi-deployment.md); two of them are load-bearing.

**Audio output stays on USB, not the Pi's 3.5mm.** A Pi 5 has no jack; a Pi 4's
is PWM into an RC filter with load-dependent distortion and a ~19 kHz rolloff.
The stimulus is the measurement's denominator, so that distortion would sit
inside `require_linear_path` indistinguishable from a compressor -- and the
rolloff clips the 5-20 kHz timing chirp. USB also keeps an electrical loopback
possible, which a microphone-only rig needs to validate itself.

**The browser observes; it never participates.** The Pi's Wi-Fi and Bluetooth
share one radio, so dropped links are expected. A run must abort and roll back
on its own, and closing the tab must do nothing. `move_microphone()` currently
blocks on the operator and needs a timeout that rolls back rather than hanging
with the car half-written.

**Open, and cheap to settle:** the DSP-408 enumerates as a USB audio device
(`Speakers (DSP-408)`, 48 kHz). That would be a digital stimulus path with no
analog stage at all -- but a vendor app over USB-B is one of the three measured
arbitration signatures that kills the RFCOMM link, so streaming audio in over
USB while holding a control link may do the same. Measure before cabling.

### Built 2026-08-13, none of it run against air yet

The acoustic path exists end to end and has never been exercised. What is new:

| | |
|---|---|
| `SplitDevices` | Two independent WASAPI streams at 48 kHz, interface output in **exclusive** mode. Measured: WASAPI cannot open one duplex stream across a UMIK-1 and a Scarlett at any rate; MME can, by resampling, which is worse than failing. A loopback across a split clock is **refused** |
| `tuner.measure.timing` | The acoustic timing reference -- chirp, first-group detection, clock ratio, timebase correction. `TimingReference` is now NONE / LOOPBACK / **ACOUSTIC**, and the middle state refuses absolute delay while permitting relative |
| `tuner.measure.fault` | Plant a known filter in the stimulus so a bench run has a right answer and can be scored against zero |
| `PassSpread` | Per-bin disagreement between repeats -- the only instrument that sees noise *during* a sweep |
| `usable_against` | Linearity judged against a measured floor rather than against the loudest tone. Required for acoustic sessions |
| `inspect_capture` | Input headroom, reported before a sweep is lost to it |

**Two claims were refuted by measurement in the same session**, and both are
recorded in `CLAUDE.md` rather than quietly dropped: that low-frequency ambient
hides in a broadband RMS gate (it does not -- RMS is dominated by the loudest
band), and that room noise makes a linear path read as compressed (not from
broadband ambient -- the narrowband detector rejects it by 42.5 dB). The real
risk is *narrowband* interference on a test frequency, which the relative guard
is blind to by construction.

### The bench session that should come before the car

The 2.1 on the bench is a Logitech THX system: a plate amp in the sub driving
everything, fed line-level from the DSP. It cannot test sub alignment (the
crossover is internal to the amp, so the DSP cannot address the sub), but it
can test the whole acoustic loop against a **known answer**.

Order matters, and steps 1-4 emit nothing that could be lost:

1. `python tools/mic_check.py` -- streams open, idle floor
2. **Level linearity** at low volume. The plate amp very likely has a limiter,
   and `require_linear_path` exists for exactly that. Find the level where it
   passes and operate there
3. Repeatability floor with `--spacing-s`, so it spans a run rather than 30 s
4. **Does the reference arrival repeat across repeats?** This is the go/no-go.
   REW's author says the acoustic timing reference is unreliable in a car
   because of close reflections -- and a *consistently* wrong detection is
   survivable, since a constant cancels in relative delay, while an
   inconsistent one poisons every delay in the session
5. Clean sweep -> becomes the target. **Not flat, not Harman** -- the system's
   own pre-fault response, so everything the Logitech does cancels and the
   correct answer is exactly the inverse of the planted fault
6. Plant the fault, run the loop, score against zero

### Next, in order of what each buys

0. **Run the bench acoustic session above.** Nothing below is worth more
   than the first real measurement through air.
1. **The microphone, in the car.** The device path is settled and built --
   `SplitDevices`, two independent WASAPI streams at 48 kHz with the interface
   output in exclusive mode. Still to wire: the reference chirp into
   `capture_sweep` (the timing module exists and is known-answer tested; the
   capture stage that plays REF_A / sweep / REF_B is not), the plan-level
   refusal that freezes the reference channel's delay, and a `DriverCeiling`
   for the reference output. Parsing the UMIK's `Sens Factor` cal header would
   give absolute dB SPL and make the level half of the objective scoreable;
   the correction curve is already supported, the sensitivity anchor is not.
2. **The floor's other half.** Its timescale is fixed; its *magnitude* still
   depends on where the baseline sits relative to the target -- 0.0585 dB
   against one objective and 0.003 dB against another, same rig, same session.
   Nothing stops a floor measured under one objective being reused under
   another: only the session id is checked, and two objectives can share a
   session.
3. **Public write-up** for the enthusiast community: ten PEQ bands not thirty,
   the two-chip split, the mixer decode, and a verified write that is not a
   working write.

### Bench commands

```
python tools/dsp408_probe.py --address 00:13:EF:A0:09:10 enumerate
python tools/dsp408_probe.py --address <MAC> idle --seconds 120
python tools/dsp408_probe.py --address <MAC> noop-write  --snapshot-out <f> [--apply]
python tools/dsp408_probe.py --address <MAC> stage5      --snapshot-out <f> [--apply]
python tools/dsp408_probe.py --address <MAC> stage6      --snapshot-out <f> [--apply]
python tools/dsp408_probe.py --address <MAC> preset --slot 6 --confirmed-by <who> --snapshot-out <f> [--apply]
python tools/tune_run.py measure       --address <MAC> --output 1 --level-dbfs -20
python tools/tune_run.py predict-check --address <MAC> --snapshot-out <f> [--apply]
python tools/tune_run.py loop          --address <MAC> --slot 6 --confirmed-by <who> \
    --driver-ceiling-dbfs -12 --ceiling-basis <what is connected> --snapshot-out <f>
```

**Run `enumerate` first, every time.** It is read-only and it is the cheapest
test for another control transport holding the device.

### ⚠ Three arbitration signatures, all measured

| Contender | What it looks like |
|---|---|
| Vendor app over USB-B | RFCOMM link opens, then **total silence** |
| Phone with the app open | RFCOMM connect **times out**; the link never opens |
| Phone connected, app closed | Link works, but **~23 % of transactions time out and retry** |

A clean session is 31/31 with `clean=True`. Anything else, suspect a second
controller before suspecting the code.

### Two bugs worth not repeating, both mine, both found on hardware

- **One path for two snapshots.** `--snapshot-out` was passed as both the
  teardown restore point and `plan.snapshot_path`, so the run's ARM overwrote
  ours and the teardown faithfully "restored" the device to the perturbed
  state. Two distinct paths now.
- **A teardown that assumed it was armed.** SETTLE disarms writes on
  acceptance, so the `finally` restore raised `WritesNotArmed` -- after the
  run, with the perturbation still live. Re-arm in the teardown.

Both left a -6 dB notch on OUT1, and both were undone using the exact original
bytes from the write journal. That is what the journal is for.

## The bring-up record, 2026-08-11

**Our code has changed the real DSP-408 and put it back.** The closed loop's
last missing half — writing — is proven end to end for a single block.

| | Result |
|---|---|
| **Read** (first contact) | 31/31 transactions, firmware `MYDW-AV1.06`, all eight 296-byte records, zero framing resyncs |
| **Stage 2** — idle survival | **Survived 120 s of silence** and answered a read afterwards. No keepalive needed on that timescale. A **lower bound**, not the timeout |
| **Stage 4** — the no-op write | 8 bytes to OUT1 block 31, the device's own payload. Fragmented **24 → 2×20 link chunks, 16 pad bytes**. Acked `0x51`. Device byte-identical afterwards |
| **Stage 5** — a real write | `gain_raw` 500 → 490 on OUT1 (−10.0 → −11.0 dB). Readback confirmed it. The other six bytes of the block — mute, polarity, delay, eq_mode, spk_type — **preserved**. Rolled back in **2.7 s**, then verified byte-identical to the first-contact snapshot taken hours earlier |
| **Stage 6** — multi-block, multi-channel, the gang | **46 writes across 4 channels**, every part restored and verified before the next began. 7 blocks on one channel through the production `write_channel`; 13 across two; the gang written with `modify_block_mirrored` and **read back holding one tune**. Device byte-identical to first contact afterwards |

Three things each of those bought, which are easy to blur together:

- **Stage 2** unblocks the tuning loop's shape. A measurement sweep leaves the
  link idle for tens of seconds and the session layer had been *assuming* that
  was fine.
- **Stage 4** proves the write **transport** — fragmented multi-chunk
  transmission over live RFCOMM and the ack under our pacing. Capture replay
  could never show this; replay is evidence about frame *construction*.
- **Stage 5** proves the write **effect**, and separately proves the
  **rollback**, which is the operative half of the improvement invariant. A
  restore that is trusted rather than re-read is not a restore.
- **Stage 6** proves the write path at the *scale a tune actually uses* — the
  backend's own `write_channel`, several channels, the blast-radius caps above
  one — and settles the gang on silicon rather than by inference from the
  capture.

> ### ⚠ OUT1 cannot currently be tuned, and this is operator-actionable
>
> Found by Stage 6's pre-flight, on hardware and in rehearsal alike.
>
> OUT1's stored EQ band 3 is `freq` 2514 Hz, `bw` 42. Under EXCLUSIVE a fit
> flattens it, and the flattened block — `d2 09 58 02 2a 00 00 00` at
> `bluetooth_device_id` 4 — produces a frame whose **checksum computes to
> zero**. The vendor app never sends one of those and this project refuses to,
> so **the whole channel is unwritable until one of those numbers moves.**
>
> One channel in eight. It is refused cleanly with nothing transmitted, so it
> is an obstacle rather than a hazard. **The fix is a one-click change in the
> vendor app** — nudge OUT1 band 3's frequency by 1 Hz (2514 → 2515) or its
> bandwidth by one step; both are inaudible and either shifts the checksum.
> Worth doing before the first in-car tune rather than discovering it there.

### The measurement loop, electrical, same evening

DSP on the bench, no speakers: Scarlett Line Out L → DSP RCA in 1, DSP RCA
out 1 → Scarlett input 2. OUT1, whose 450–3500 Hz passband sits inside the
band independently validated against REW.

| | |
|---|---|
| interface rate | 44 100 Hz (**the DSP's 48 kHz is a different number** — see below) |
| stimulus ceiling | −20.0 dBFS, derived live from the channel's own gain and EQ |
| level linearity | 0.27 dB spread across level — linear |
| **session repeatability floor** | **0.0585 dB** rms over 3 sweeps, 450–3500 Hz |

The floor is six times tighter than the 0.39 dB wideband figure quoted
elsewhere in this project, because that one spans the HF region and this is
one channel's midrange passband. **It is a per-session number and this one
belongs to this session only.**

Then the experiment that matters most, `tune_run predict-check`: one known
band written **through our own backend**, measured differentially, compared
against `biquad.response_db`.

| | |
|---|---|
| requested / achieved | 1 kHz, −6.0 dB, Q 2.00 → Q 1.983 after quantisation |
| measured / predicted depth | −6.08 dB / −6.00 dB |
| **rms error, 450–3500 Hz** | **0.065 dB** over 300 points |
| max error | 0.298 dB |
| level drift between sweeps | −0.002 dB |

**0.065 dB against a 0.0585 dB floor: the model and the device cannot be told
apart on this rig.** That closes the largest open risk in the project — an
optimizer converging against a model of a system it has stopped resembling.
Full reasoning in `CLAUDE.md`.

Two things worth carrying:

- **The interface rate is not the DSP rate.** 44 100 vs 48 000. The biquad
  runs in the ADAU so its response is predicted at 48 kHz; the capture runs on
  the Scarlett at 44.1 kHz. Hardcoding 48 kHz for the interface failed loudly
  with `Invalid sample rate`, which was lucky — the reverse mistake, predicting
  at 44.1 kHz, is silent and grows with frequency.
- **`Speakers (DSP-408)` is a trap.** It appears in the Windows device list
  whenever our Bluetooth control link is up, because the DSP exposes an A2DP
  sink. It is SBC-compressed and would wreck a measurement while looking like
  the obvious device to choose. The standing rule is never select by index;
  this is the case where the *name* is the bait.

### ✅ UI register, round 1 worked 2026-08-12

Ten questions, one sitting. Nine confirmations, **one contradiction**, and the
contradiction was worth more than the nine.

**Decoded block 33 = MIX** from the mixer screenshots: eight bytes, one per
input, 0-100, only four used on a four-input device. The live routing it
reports matches `docs/hardware.md`'s reachability table exactly -- and that
table was derived independently on the bench by sweeping outputs and listening
for silence. Two routes, no shared reasoning.

**Killed the `DataType 3` lead.** "iOS shows input values 0-100", recorded the
previous day as the first evidence of a vendor path to the unmapped input
section, was the mixer all along. Both apps have it. The input section remains
unreached by any vendor app.

**Reframed blocks 34/35** as almost certainly vestigial fields from a shared
codebase for a larger sibling product -- constant on every channel, no UI
anywhere, and block 33 carrying eight input slots on a four-input device. The
refusal stands, for a better reason.

Full results in [ui-question-register.md](ui-question-register.md); the
protocol consequences are in `CLAUDE.md`.

**Still open and small**: the Q range (the last unbounded `FitConstraints`
value), and whether bands 1 and 10 carry a shelf toggle the middle bands lack
-- three `.DDP` files say they do, the operator's read of the UI says they do
not, and that disagreement points at exactly where to look.

### ✅ Answered 2026-08-12: **ten EQ bands, not thirty**

The community has held for years that the DSP-408 has 30 usable PEQ bands per
channel. Measured on the bench, it has **ten**.

| Slot | Measured | Predicted | Verdict |
|---|---|---|---|
| **1** | **−6.04 dB** | −6.00 dB | live, 0.062 dB rms |
| **11** | −0.22 / −0.43 dB, and −0.007 dB by absolute sweep | −6.00 dB | **inert**, three runs |
| **31** | −0.25 dB | −6.00 dB | **inert** |

Slot index maps straight to hardware biquad index -- no compaction, since slot
11 did nothing with nine free slots below it. The record is almost certainly
sized for a larger sibling product, the same explanation as block 34's
`MIX_IN_9_16` name and block 33's eight input slots on a four-input device.

**The dangerous part, and the reason to publish it:** the failure is silent. A
band written to slot 11 acks, reads back byte-exact, and appears in the `.DDP`.
**On this device a verified write is not a working write** -- every check this
project makes on a write passes on a band that does nothing.

`max_peq_per_channel = 10` is now measured *acoustically* rather than observed
in the UI. `ADDRESSABLE_BANDS` stays 31: it is a true fact about the record,
and keeping the two separate is what made the question testable.

> **A metric nearly manufactured a finding.** Two runs reported a +1.888 dB
> "level shift", reproducing to 0.004 dB. It was `mean(measured − predicted)`
> mislabelled as level drift: with a notch predicted and nothing measured it
> returns −mean(predicted), which is −1.887 dB. Caught by a control on a
> known-good band, an absolute three-sweep measurement, and one line of
> arithmetic. **A derived statistic reproducing precisely shows the derivation
> is deterministic, not that the effect is real.** Full account in `CLAUDE.md`.

### ▶ Then, in order

All the desk work behind the closed loop is done as of 2026-08-12:
`DeviceLimits` corrected to static per-channel delay, the pre-existing-EQ
double-count fixed, block 33 decoded and cross-checked, the bandwidth domain
pinned, and the fit constraints labelled ours-versus-the-device's.

**The next action is the electrical closed loop** on the rig as it is wired --
`tuner.orchestrate` driving the real backend and a real measurer for the first
time. Known-answer shape: measure OUT1, make that the target, write a
deliberate perturbation, require the tuner to remove it. Judged against this
session's measured floor rather than zero.

### Resource limits, answered by asking (2026-08-12)

Operator, from the vendor app: **delay maxes at 8 ms per channel** (= 384
samples at 48 kHz; also displayed as 277 cm / 109 in, which agree), and
**10 PEQ bands per channel**, not the 31 the protocol addresses.

That closes the program-space / delay-RAM question that had been carrying
three candidate routes (SWD dump, datasheet, bench binary-search), none of
them cheap. Full consequences in `CLAUDE.md` — including that the shared-pool
delay model is probably wrong and that `max_peq_per_channel = 10` was right
for the wrong reason. **Nothing has been changed in code yet.**

**Confirmed 2026-08-12: 8 ms sets on every output at once — there is no
shared pool.** Static per-channel allocation, 384 samples each, 1536 per chip.
`delay_samples_per_chip = 1024` and the pooled accounting in `optimize.budget`
are both wrong; neither has been changed yet.

**And a finding nobody would have thought to ask for: the channel link mirrors
gain only.** Delay moves independently on linked outputs 7/8, by design — the
DSP is agnostic to why two channels were linked. So `linkgroup_num` means
*mirror the gain slider*, not *these are one acoustic source*. Our gang write
is deliberately broader than the app's, and should stay that way: two drivers
in one enclosure need matched delay too, and the app will happily let an
operator set one and forget the other.

Still open, and not UI-answerable: what the device does with an out-of-range
delay. One bounded bench write.

### What is still unproven on silicon

Stated narrowly, because this is the claim that erodes:

- ~~**Preset recall as a restore path.**~~ **Proven 2026-08-12** — store to
  slot 6, perturb, recall, device byte-identical. See the callout in
  `CLAUDE.md`, including two side findings: slots 7-15 are confirmed a
  name-echo rather than storage, and a rollback-by-recall leaves the scratch
  slot as the running slot, which the next run's ARM correctly refuses.
- **All eight channels in one run.** Stage 6 wrote four.
- **A write while audio is playing.** Every write so far was into silence.

> ### ✅ ~~Blocker for the closed loop~~ — fixed 2026-08-12
>
> `TuneRun` fits against a baseline measured **with the channel's existing EQ
> loaded**, then writes `EXCLUSIVE`, which replaces that EQ — so it is counted
> twice. Demonstrated offline: a channel pre-loaded with +8 dB at 1200 Hz ends
> up **5.9 dB** from where the same run starting flat lands, and **both report
> `accepted`**. Every channel on this car has EQ loaded.
>
> **Fixed** by `TuneRun._without_existing_eq`, which subtracts the modelled
> response of the loaded EQ before fitting. The baseline stays the measured
> one, so the improvement invariant still compares against the operator's real
> tune; only the fit sees the raw response. Flattening the channel first was
> the alternative and was rejected because it destroys that baseline and costs
> a second sweep per source.
>
> **The regression test found something bigger than the bug.** Reverting the
> fix and measuring each candidate assertion: the objective score moved
> 0.233 dB and the rms deviation 0.735 dB while the response was **6.30 dB**
> out. An rms objective is nearly blind to narrowband error — which is exactly
> why the invariant accepted both runs. Full numbers in `CLAUDE.md`; the short
> version is that **the improvement invariant is necessary and not
> sufficient**, and a regression test for a narrowband defect must assert on
> peak or on the defect's signature, never on the objective.

**The next action is fixing that, then the electrical closed loop**: `tuner.orchestrate` has
never driven a real backend or a real measurer, and everything underneath it
now has. Run it on the bench with the Scarlett before it ever sees the car —
same wiring as above, so a failure is an orchestration bug rather than an
acoustics one.

The known-answer shape for that run: measure OUT1, make **that** the target,
write a deliberate perturbation, and require the tuner to remove it. The right
answer is known in advance, no published curve has to be invented, and the
improvement invariant has a real floor (0.0585 dB) to judge against.

Everything buildable without hardware is built. The bench session's run sheet
([next-bench-session.md](next-bench-session.md)) is a record, not a plan.

The commands that produced the table above, for reference and re-running:

```
python tools/dsp408_probe.py --address 00:13:EF:A0:09:10 enumerate
python tools/dsp408_probe.py --address 00:13:EF:A0:09:10 idle --seconds 120
python tools/dsp408_probe.py --address 00:13:EF:A0:09:10 noop-write --snapshot-out snapshots/<date>.json [--apply]
python tools/dsp408_probe.py --address 00:13:EF:A0:09:10 stage5     --snapshot-out snapshots/<date>.json [--apply]
```

Each write command is a dry run without `--apply`, printing the exact bytes,
and captures its own restore point **in the same invocation** — a snapshot from
an earlier session is evidence about an earlier session. Both target OUT1 block
31, the block every one of the capture's 21 writes went to.

`stage5` is hard-wired to the one transition the capture contains and **refuses
to run from any other starting `gain_raw`**, exiting 2 without arming. There is
deliberately no general set-a-parameter command in this tool: it should not own
the ability to write arbitrary bytes to arbitrary blocks until the narrow case
is proven. `tests/test_probe_tool.py` covers the refusal, including that it
happens *before* arming rather than merely before transmitting.

**Run `enumerate` first, every time.** It is read-only and it is also the
cheapest test for another control transport holding the device: the measured
signature of a live USB-B session is that the RFCOMM link opens and then
nothing ever answers, which `session.SuspectedUsbArbitration` now names.

> **Why "outputs disconnected" is not ceremony for a no-op.** A no-op is inert
> only if it lands where addressed. A mis-addressed write puts OUT1's misc
> block — `gain_raw` 500, delay 144 — onto another channel, and on OUT7/8 that
> is 370 → 500, **+13 dB into a subwoofer sharing a ported box**. The readback
> catches it afterwards; the drivers would have had it already. The rule that
> the first writes happen with nothing connected is load-bearing even for the
> write that cannot change anything.

Two independent threads are live: **the DSP** (the critical path, and where
this session went) and the **capsule board build** (paused — see near the end).

---

## One-paragraph summary

The measurement engine works and is now validated **against REW** — 0.35 dB max
and 0.09 dB rms over 30 Hz–3.5 kHz, a second implementation sharing no code —
on top of the analytic known-answer tests and the electrical loopback. The
DSP-408's control protocol is decoded and **validated byte-for-byte against
61 742 frames of real device traffic**, and the whole control stack exists:
framing, a transmit allow-list, transports, an in-process fake, lock-step
sessions, read-modify-write, snapshot/restore, and **preset store and recall**,
which is the rollback the improvement invariant has always described. Target
curves, PEQ fitting and time alignment are built, and as of 2026-08-10
**`tuner.orchestrate` joins them into a closed loop** — arm, floor, baseline,
fit, write once, re-measure, accept or roll back — rehearsed against the fake
device through every outcome it can produce.

**Our code reads, writes, tunes and rolls back a real DSP-408**, proven through
every bring-up stage and a full closed-loop run on 2026-08-12. Rollback exists
by three independent routes -- block-by-block, preset recall, and a `.DDP` load
through the vendor app -- and the first two are proven on hardware. See the
header of this file for what is and is not established.

**Only one control transport at a time.** Measured 2026-08-11: with the vendor
app connected over USB-B the device accepts the Bluetooth RFCOMM link and then
ignores it completely — no reply, no error, no disconnect. Detach USB before
opening the socket.

`pytest` → **1508 passing**, `ruff check .` → clean, `dsp408_probe rehearse` →
29/29, `tune_run rehearse` → 41/41. Everything runs with no hardware attached.

### What the bench session settled

Sixteen `.DDP` A/Bs, two HCI captures, and a REW comparison. Fifteen protocol
questions closed, listed below; **the table of what is still open follows it and
is longer than one row.** (This sentence read "every open protocol question
except one is now closed" until 2026-08-11, directly above a table of six.)

| Question | Answer |
|---|---|
| RBJ filter shape | Half-gain convention, ±0.8 % across a 4.6× bandwidth span, symmetric in boost and cut |
| `fc` at 25 Hz | Continuous — 25/27 Hz fit to 24.9/26.9, exactly 2.0 Hz apart |
| Preset store / recall | `user_id` selects the slot; **a recall is eight READs**, no select opcode |
| Preset slot count | **Six**, not fifteen — 7–15 are a stale buffer |
| Crossover slope | `level` byte, `slope = 6 × (level + 1)` — **now writable** |
| Crossover alignment | 0 Linkwitz-Riley / 1 Butterworth / 2 Bessel / 3 Defeat |
| `EqBand.type` | 0 PEQ / 1 low shelf / 2 high shelf — **added a refusal** |
| Mute on the wire | Ordinary `dt4/id31` block write, byte 0 |
| Master volume | `dt9/ch5/id0` byte 0 — global, one write moves every channel |
| Link mirroring | **The app mirrors, the device does not** — 6 writes linked, 3 unlinked |
| Clean disconnect | **There is none.** Polling stops mid-stream |
| `.DDP` load | Reaches the device; USB-only, no Android import |
| `DataType 3` | Unreachable from the Android app — that is why it never appeared |
| Measurement engine | 0.35 dB max / 0.09 rms against REW |
| HF artifact | MME below 10 kHz; REW's own scatter above it. Ours: 0.080 dB rms to 18 kHz |

**Still unknown, and none of it closable by another A/B:**

| Unknown | Why it is still open | When it bites |
|---|---|---|
| Blocks 34/35 | The decompiled app and the device's readback contradict each other. Needs a resolution, not more samples | Any write path through them — refused everywhere |
| ~~Channel-to-chip mapping~~ | **Settled 2026-08-11 by logic analyser.** `0x37` drives outputs 1-4, `0x35` drives 5-8 | `DeviceLimits` now models two per-chip pools |
| **Does the link need the 10 Hz poll?** | Both captures show the app polling regardless of interaction, which records *the app's* behaviour and not *the device's* requirement | **Not at first contact — during the first tuning run**, where minutes of measurement separate writes |
| ~~`bluetooth_device_id` semantics~~ | **Narrowed 2026-08-11.** We sent **4** (the wire value, our `OBSERVED_BLUETOOTH_DEVICE_ID` default) from a *PC* pairing and the device answered all 31 transactions. So it is not a per-host index that a different pairing must change, which was the live hypothesis. Still unknown: whether **0** also works | Nowhere yet. It would bite only if a future host had to differ |
| 20-byte padding: required or tolerated? | **Tolerated on both paths, 2026-08-11.** All 31 reads were padded and answered; Stage 4's write was padded, fragmented 24 → 2×20 with 16 pad bytes, and acked. **Whether it is *required* is still open** — we have never successfully sent an unpadded frame, and the one we tried is confounded by the USB-arbitrated window, so its silence proves nothing | Nowhere urgent. It would bite only if a future transport could not pad. Answering it costs one unpadded read on a healthy link |
| Device error behaviour | `0x52` has never been seen. The *contract* is specified; the device is unmeasured | First fault |
| Preset store coverage of global/input state | Unknown whether a store captures `dt9` globals or the `.DDP` input sections | Rollback fidelity |

**The keepalive one is the trap**, because it is the only entry that will not
show up during bring-up. `session.poll_status` documents the uncertainty
honestly — "do not assume it is a required keepalive, and do not assume it is
not" — but the session layer currently *embodies* an answer by not sending
one. Bring-up Stage 2 (connect, send nothing, time the drop) is what settles
it, and it must happen before any run that leaves the link idle for minutes.

**Built 2026-08-11 as `Dsp408Session.measure_idle_survival`, run by
`dsp408_probe idle --seconds N`.** Deliberately passive: probing inside the
window would supply exactly the traffic being tested for, so it watches the
transport for an unsolicited close and only then attempts one read.
`IdleSurvival.survived` requires **both** halves — the socket never closed
*and* it still answers — because an open socket that has stopped answering is
the failure this device demonstrably produces under USB arbitration.

**One run gives a lower bound, never the timeout**, and the tool says so in
its own output. Ladder it across separate runs (30 s, 120 s, 300 s) to bracket
the answer; a single pass can only ever show that a timeout is longer than
what was tried.

### The adversarial review, and what it changed

An external review challenged the project's claims; the full scorecard is in
[review-2026-08-09.md](review-2026-08-09.md). Fifteen claims: **ten confirmed,
four refuted, one partial.** The refutations matter as much as the
confirmations — the timing-reference rule, the provenance refusal and
`require_linear_path` were all alleged unenforced or broken and all are fine,
so conceding on authority would have caused three regressions.

What it did establish, worst first:

1. **The rollback story is circular.** `ddp.py` is parse-only, no preset opcode
   is known, and nothing in `src/` implements rollback at all. The improvement
   invariant's "automatic rollback verified by re-measurement" is a policy with
   no mechanism behind it.

   **Resolved 2026-08-09.** The mechanism was measured on the wire — store the
   baseline to a preset slot, and one recall restores all eight channels in
   ~5 s, proven by known answer — and then implemented:

   | Layer | What it gained |
   |---|---|
   | `protocol.py` | `PRESET_SLOT_MAX`, the `user_id` hazard documented on the field itself |
   | `txpolicy.py` | `allow_presets`, and a refusal for slot-addressed frames that are not exactly the observed shape |
   | `session.py` | `recall_preset`, `store_preset`, `read_preset_name` |
   | `snapshot.py` | `store_as_preset`, `restore_from_preset`, `PresetRestoreReport` |
   | `fake_device.py` | slot storage, and a read that mutates — so code meets no surprises on the real unit |

   `restore_from_preset` re-reads all eight channels and compares against the
   expected snapshot; **that comparison is the only evidence the rollback
   worked.** The recall returning eight records is not, because the device
   answers with the slot's contents whether or not it applied them.

   `.DDP` files also load back to the device, giving a second, independent
   restore path. What remains is joining rollback to the tuning run, which is
   M4's orchestration rather than a rollback gap.
2. ~~**The RBJ filter shape is unmeasured and load-bearing.**~~ **Measured
   2026-08-09 and confirmed** — half-gain convention, ±0.8 % across a 4.6×
   bandwidth span, symmetric in boost and cut. The review was right that it was
   load-bearing and unmeasured; it is now measured. See D1 below.
3. ~~**REW goldens never existed**, despite the validation policy asserting them.~~ **Built 2026-08-09** — and they found things: the HF artifact was MME, and the reference turned out noisier than the engine it was checking.
4. Program space is not modelled at all; named safety defences had no code.

Its best suggestion was procedural: mine the *existing* capture for the session
layer before writing any transport. That is what produced most of this session's
findings, at zero risk and no hardware.

**Two gaps the review missed**, found while checking it: `group_delay_samples`
was reachable without a timing reference, and `IncomparableProvenance`'s raise
branch had no test. Both closed.

### What this session established

**The transport was wrong.** It is **classic Bluetooth RFCOMM (SPP), server
channel 1** — not BLE. An HCI capture of the vendor app shows 2918 protocol
frames over RFCOMM and five packets touching BLE ATT, none of them ours. M3 is
a serial socket, not a GATT client; `bleak` is not a dependency.

**The codec is validated against ground truth.** All 5834 frames in the capture
re-encode byte-identically through `tuner.dsp.protocol`
(`tests/test_golden_frames.py`, fixture in `tests/golden/`). This is the
validation the project's policy demanded before any backend transmits, obtained
without writing a byte to the device.

**All five parameter scalings are measured**, and now confirmed on the wire:

| Field | Mapping |
|---|---|
| `delay_raw` | integer samples at 48 kHz |
| `gain_raw` | `dB = raw/10 − 60` |
| EQ `level` | same encoding, 600 = 0 dB |
| EQ `bw` | `octaves = (raw + 5)/100`, displayed as Q |
| frequency | Hz, 1 Hz resolution, **not quantized** — measured, 450 Hz reads 449.4, and **continuous at 25 Hz too**: 25 / 27 Hz fit to 24.9 / 26.9, separated by exactly 2.0 Hz |

**A write carries a whole 8-byte block, never one field**, so a backend must
read-modify-write. Writes are immediately non-volatile and there is **no undo**;
preset recall is the only device-side restore, **and it is now decoded**: a
recall is eight READs of `data_id` 0 with `user_id` set to the slot, with no
select opcode anywhere. Implemented in `session.py` and `snapshot.py`.

**The session layer is measured** (new 2026-08-09; full detail in
`dsp408-protocol.md`). The dialogue, not just the grammar:

- **Everything moves in 20-byte zero-padded chunks.** All 2918 host frames start
  on a 20-byte boundary; every inter-frame gap is `0x00`.
- **Strict lock-step**, one outstanding request, 2916/2916 transactions. READ →
  `0x53` with data; WRITE → `0x51` bare ack. **`0x52` ERROR never observed.**
- **Replies echo the request header bit-for-bit** — 0 mismatches in 2916 pairs —
  so match on the echoed tuple, not on ordering.
- Reads median 47 ms / max 339 ms; writes median 85 ms / max 354 ms. Pacing is
  reply-driven, ~10 req/s sustained.
- **Connect ritual: 31 fixed transactions, 5.8 s, no auth.** Firmware
  `MYDW-AV1.06`; current preset slot; 15 preset names; then 8 bulk reads.
- **`DataID 119` returns a whole 296-byte channel** — the snapshot primitive.

**The bulk record agrees with the vendor app byte for byte.** All eight
296-byte records equal the output section of three `.DDP` backups — 2368 bytes,
zero differing, two paths sharing no code. Pinned by `tests/test_bulk_record.py`.
This is genuine independent ground truth for the mechanism M3's rollback rests
on, and it cost nothing.

**⚠ Blocks 34/35 must not be written.** `protocol.OutputBlock` calls 34
`MIX_IN_9_16`; the device's readback returns dynamics-shaped bytes there on
every channel, and `ddp.py` has always called it "dynamics A" — the two modules
have disagreed since both were written. 34 is *not* a copy of 35 either: they
match on channels 0–5 and differ on 6/7 in the `linkgroup_num` byte alone. Both
are in `protocol.UNVERIFIED_OUTPUT_BLOCKS` and excluded from every write path.

**`bluetooth_device_id` is 4 on the wire, 0 in our default** — and it is inside
the checksum, so a frame we construct differs from one the app sends.

**Byte 0 of the MISC block is `enabled`, 1 = on** — the opposite sense to the
`mute` name the decompiled app gives it. Settled by an operator A/B that changed
**exactly one byte in the whole backup**, with `gain_raw` untouched, so muting is
a separate control rather than a gain zeroing. `protocol.py` renamed;
`Dsp408Spp` writes it. Pinned by `tests/test_bulk_record.py`.

**The unit was opened and photographed** (`Board Images/`, decoded in
`docs/board-probing.md`). Two inherited facts corrected: the MCU is a **Geehy
APM32F103** clone, and there **is an EEPROM** on the DSP card. The output stage
is inverting MFB filters on a split supply. **The channel-to-chip mapping was
attempted with a meter and not resolved, and no guess is recorded.**

**`tuner.optimize.biquad` is implemented** — coefficients agree with
`scipy.signal.freqz` to 2e-13 dB, and a planted peak cancels to 0.003 dB rms.

---

## Milestones

| | Scope | State |
|---|---|---|
| **M0** | Control protocol spike | **Done.** Transport measured (RFCOMM, not BLE), wire format decoded and validated against real traffic, **session layer measured**, backup-file format decoded, all five parameter scalings measured. |
| **M1** | Measurement engine | **Done**, and validated against REW: 0.35 dB max / 0.09 rms over 30 Hz-3.5 kHz (`tests/test_golden_rew.py`), on top of the analytic known answers and the ±0.35 dB electrical loopback. `verify_simultaneous_capture` is an **M1-extension gate for multichannel interfaces**, not a gate on the single-channel rig that validated M1; it stays stubbed and does not reopen the milestone. |
| **M2** | Microphone calibration rig | Not started. Non-blocking *only* for single-mic magnitude work on the Scarlett; it gates the capsule array and any absolute-SPL target. |
| **M3** | DSP control backend | **Proven on hardware through Stage 6, 2026-08-11.** Reads 31/31, firmware `MYDW-AV1.06`, all eight records, zero resyncs; link survives 120 s of silence. Writes: the Stage 4 no-op (fragmented 24 -> 2x20, acked `0x51`); Stage 5's real change with a rollback verified by re-reading; Stage 6's 46 writes across four channels, including the backend's own multi-block `write_channel` and `modify_block_mirrored` read back holding one tune. Device byte-identical to a morning snapshot after every stage. **Unproven: preset recall as a restore path, all eight channels in one run, and any write while audio plays.** `RfcommSocketTransport` has touched hardware; `SerialPortTransport` has not. |
| **M4** | Closed tuning loop | **Ran end to end on hardware 2026-08-12 and accepted.** Arm, isolation, floor, baseline, fit, write, verify, settle -- on an electrical bench rig, device restored and verified byte-identical afterwards. The objective is fingerprinted at plan time and re-hashed before the verdict; gangs are declared in the plan with a basis and checked by readback; isolation reads mute states back from the device and proves silence by measurement. `tune_run rehearse` covers all six outcomes against the fake, 41/41. Its known-answer run scored 1.034 where ~0 was correct; the cause was the fit's cost demanding an absolute level match a peaking chain cannot make, **not** the `max_cut_db` boundary first blamed for it, and it was fixed on 2026-08-13 (0.814 -> 0.008 dB rms on the reproduction, one band instead of ten). Remaining: no microphone has ever been in the loop, all-eight-channels in one run, and writing while audio plays. |

---

## What exists and works

**Measurement** (`tuner.measure`) — log sweep + matched inverse, deconvolution,
gating, frequency/phase/group-delay/RT60/spatial averaging, REW-format cal
files, and an end-to-end `capture_sweep()` that does safety ramp → I/O →
deconvolution → alignment → provenance.

**Rig verification** (`tuner.measure.qa`) — three checks plus median-of-N.
`require_quiet_path` (idle floor) and `require_signal_response` (the stimulus
actually arrives) run automatically inside `capture_sweep`; median-of-N always
runs. **`require_linear_path` does not run automatically** — it costs ~14 s, so
it stays a per-session operator responsibility until it is wired to
`SessionInfo`. All three were earned on the bench, not imagined.

**Safety** (`tuner.safety`) — level ceilings, ramping, clip/DC abort, and
`ceiling_for_device_state`, which is hard safety rule 6 as code: the driver's
ceiling less whatever channel gain and EQ boost the DSP adds downstream of the
limiter. Reachable from any backend as `stimulus_limit(output)`, which reads
the device **live** every time — caching it would be the natural optimisation
and the wrong one, since the closed loop's whole shape is *write a boost, then
sweep the channel you just boosted*.

**Orchestration** (`tuner.orchestrate`, new 2026-08-10) — the closed loop.
`plan.py` freezes the run's inputs (scratch slot with no default, per-output
`DriverCeiling` with a required basis), `objective.py` is the scalar and its
SHA-256 freeze, `run.py` is the staged run and its report, `rig.py` is the
never-run adapter to `capture_sweep`. See the milestone table and §1 of "what
to do next".

**DSP** (`tuner.dsp`):

- `protocol.py` — complete frame codec, **validated byte-for-byte against real
  device traffic**. Encode/decode, XOR checksum, EQ bands, all output parameter
  blocks, destructive-opcode blocking, and the measured unit conversions
  (`gain_dbfs`, `gain_raw_for`, `bandwidth_octaves`, `q_from_bw_raw`,
  `bw_raw_for_q`).
- `ddp.py` — reader and differ for the vendor app's `.DDP` backup format. The
  zero-risk readback channel that settled most of M0.
- `btsnoop.py` — Android HCI snoop log reader. Parses btsnoop records, ATT PDUs
  and **RFCOMM byte streams**, and recovers protocol frames from any of them.
  Streams are per-DLCI; RFCOMM's control channel is excluded.
- `framing.py` *(new 2026-08-09)* — `FrameReader` (preamble resync, zero-pad
  tolerance, partial buffering, `MAX_PAYLOAD` bound) and `chunk_20`. **Replayed
  against the capture**: recovers all 2916 device frames under randomized
  fragmentation, and re-chunking the decoded host frames reproduces the entire
  58 780-byte host stream byte for byte.
- `txpolicy.py` *(new 2026-08-09)* — the transmit **allow-list**, replacing an
  inert blacklist. All 2918 captured host frames pass; writes are refused
  unless armed, and blocks 34/35, `data_id 119`, `DataType 3`, all `DataType 9`
  writes and linked channels are refused outright. Includes `BlastRadius`.
- `transport.py` *(new 2026-08-09)* — the byte pipe, kept deliberately thin
  because it is the only layer that cannot be tested without hardware.
  `LoopbackTransport` (in-process), `ReplayTransport` (strict capture oracle),
  `RfcommSocketTransport` (`AF_BLUETOOTH`, works on Linux **and** Windows), and
  `SerialPortTransport` (pyserial, lazily imported, not a hard dependency).
  The two real ones are **untested against hardware**.
- `fake_device.py` *(new 2026-08-09)* — an in-process DSP-408 holding real
  state: eight 296-byte records, whole-block writes, the measured persistence
  model (power cycle preserves, preset recall destroys), the connect ritual's
  replies verbatim, and fault injection for the paths the capture cannot cover.
  **Refuses to improvise** — an unrecognised request raises rather than
  returning a plausible reply.
- `session.py` *(new 2026-08-09)* — lock-step transactions. One outstanding
  request, replies matched on the echoed header tuple, reply-kind validation,
  measured pacing, single-retry-on-timeout, and `handshake()` replaying the
  vendor app's 31-transaction connect ritual. **Driven against the capture, our
  first 31 requests are byte-identical to the app's** (`test_session_replay.py`),
  as are all 21 parameter writes.

  Its `FaultPolicy` writes the error contract down *before* the first fault:
  `0x52` halts and poisons the session and is **never** retried, because it was
  never observed and retrying an unknown error is how you learn what it meant
  the expensive way.
- `device.py` *(new 2026-08-09)* — whole-channel records and **mandatory
  read-modify-write**. There is no `write_field`; `modify_block` reads,
  mutates, writes and verifies. Verification compares the **whole 296-byte
  record**, so an off-by-one `data_id` landing correct bytes eight bytes away
  is caught — a per-block check cannot see it. Also `WriteJournal`
  (fsynced *before* transmission) with `reconcile()`, two-key `arm_writes()`,
  and refusal of blocks 34/35.
- `snapshot.py` *(new 2026-08-09)* — **the rollback mechanism.** Records stored
  verbatim, never as decoded fields, because blocks 34/35 are undecoded and
  several encodings are unknown. `capture` / `save` / `load` / `compare` /
  `restore`, atomic writes, digest verification, and `to_ddp()` for a restore
  path through the vendor app that shares no code with ours. `restore` defaults
  to `dry_run=True`.
- `dsp408_spp.py` *(implemented 2026-08-09)* — the engineering-units backend.
  Sets gain, delay, crossover **frequencies** and PEQ bands; carries through
  polarity, `spk_type`, `eq_mode`, the mute/enable byte, the crossover filter
  and slope bytes, mix, dynamics, name and link group. **Raises rather than
  silently ignoring** anything it cannot honour — a dropped field would leave
  the device not matching the model the optimizer reasoned about. `PeqPolicy`
  has no default, so construction fails until the caller chooses between
  leaving unmentioned bands alone and flattening them.
- `ddp.serialize` / `ddp.splice_outputs` *(new 2026-08-09)* — the `.DDP`
  writer. `serialize(parse(x)) == x` for all fourteen backups in the repo, and
  splicing the capture's records into a matching backup reproduces it byte for
  byte.
- `sim.py` — the default backend. `dsp408_spp.py` **has read the real device
  and has transmitted one no-op write to it** (2026-08-11), with the BLE
  constants kept as a fenced dead end. (This bullet said "is a stub" until
  2026-08-09, "never transmitted" until first contact on 2026-08-11, and "never
  written to it" until Stage 4 the same evening; each was written before the
  session that made it false. Four revisions, one pattern.)

**Optimizer** (`tuner.optimize`) — `biquad.py` fits PEQ chains: RBJ
coefficients, chain response, a public `objective`, and a differential-evolution
fit that searches frequency continuously and bandwidth on the device's integer
grid. `verify.py` implements the improvement invariant.

`target.py` *(new 2026-08-09)* — target curves, and the **level/shape split**
that keeps the fitter from spending its band budget on broadband gain:
`correction_db` returns the target raised to the measurement's own level plus
the offset it removed, which is the channel gain change the tune needs.
`harman_in_car` deliberately **raises** rather than reproducing published
values from memory — a wrong target is inherited by every tune afterwards and
no measurement can reveal it.

`delay.py` *(new 2026-08-09)* — time alignment from arrival times, with a
closed-form weighted compromise across seats and `residual_spread_samples` to
report what that compromise cost. `budget.normalize_delays` implemented.

> Two honest qualifications on that line, both from the review.
> ~~**`biquad.py` is validated against itself**~~ — it was, and **it no longer
> is.** Agreement with `scipy.freqz` was our evaluator against scipy evaluating
> *our* coefficients, and the planted-peak test cancelled a curve we synthesised.
> D1 (2026-08-09) measured the device's realised filter shape against the model
> and it agrees to ±0.8 % in bandwidth and ±0.02 dB in gain. `biquad.py` now has
> independent ground truth.
> ~~**`verify.py` has never executed end-to-end**~~ — still true, and it
> still cannot until M4 joins the pieces. But **the rollback it demands now
> exists**: `snapshot.store_as_preset` / `restore_from_preset`, on a mechanism
> measured on the wire. What is missing is the run that calls them, not the
> mechanism.

**Tooling**

| Tool | Does |
|---|---|
| `tools/ddp_dump.py` | dump or diff vendor `.DDP` backups |
| `tools/bench_crossover.py` | measure a crossover corner and fit it against LR4 |
| `tools/btsnoop_extract.py` | pull protocol frames out of an HCI capture, incl. bug-report zips |
| `tools/bench_peq.py` | measure one PEQ band and test it against the RBJ model (D1) |
| `tools/dsp408_probe.py` | enumerate / snapshot / verify / diff / restore / reconcile / **idle** / **noop-write** / **stage5** / **stage6** / **preset**, plus `rehearse` and `--fake-from` |
| `tools/ghidra/` | reproduces the firmware decompilation |

**Captured evidence** — `captures/btsnoop_hci.log` (real device traffic),
`corpus/*.DDP` (41 vendor-app tune saves, mostly single-control A/Bs; see
[corpus/README.md](../corpus/README.md)), `Board Images/` (teardown
photographs). All are load-bearing: several conclusions rest on them and are
pinned by tests.

**Tests: 1508.** Of these, 209 are the protocol golden frames and 18 the bulk
record — both checked against evidence from outside this project. The rest are
largely self-referential by necessity: **the suite runs against `sim.py`, which
encodes our model of the device, including the knowingly-wrong single-pool
`DeviceLimits`.** Sim tests catch regressions against our assumptions, not
disagreement with hardware. Worth remembering before reading a green suite as
confirmation.

---

## What is decided

| Question | Answer | Where |
|---|---|---|
| Control transport | **Classic Bluetooth RFCOMM (SPP), server channel 1.** Measured from an HCI capture of the vendor app — *not* BLE, which was the earlier and wrong answer | `docs/dsp408-protocol.md` |
| Session model | **Strict lock-step, one outstanding request.** READ → `0x53` with data, WRITE → `0x51` bare ack; replies echo the request header bit-for-bit. ~10 req/s, reply-driven | same |
| Link framing | **20-byte zero-padded chunks**, every frame preamble-aligned. Required or merely tolerated is unknown | same |
| Device snapshot | **`DataID 119` returns a whole 296-byte channel.** Validated byte-for-byte against the vendor app's own `.DDP` export | `tests/test_bulk_record.py` |
| Wire protocol | **Validated against real traffic** — all 5834 captured frames re-encode byte-identically | `tests/test_golden_frames.py` |
| Wire format | `80 80 80 EE` + 10-byte header + payload + XOR + `AA` | same |
| Output parameters | DataType 4; DataID 0–30 EQ, 31 misc, 32 xover, 33/34 mix, 35 dynamics, 36 name | same |
| Chip count | **Two ADAU1701s** (teardown photos) | same |
| Bridge MCU | **Geehy APM32F103** (STM32F103 clone), bit-banged I²C. Corrected from our own photos | `docs/board-probing.md` |
| DSP-card EEPROM | **Present** — 8-pin Atmel serial EEPROM beside the MCU. The teardown said there was none | same |
| Output stage | 8× NE5532 inverting MFB reconstruction filters, IN+ hard-grounded, **split ± analog supply**, outputs AC-coupled to the RCA jacks | same |
| Measurement rate | 44.1 kHz (native; 48 k triggers Windows resampling) | `docs/hardware.md` |
| Audio interface | **Build one.** 6-in/4-out, one clock, no gain knobs, internal loopback, USB bus-powered. Designed, not built | `docs/measurement-interface.md` |
| Microphones | 4× electret capsules + bias (no phantom); UMIK-1 is the transfer standard, never an array element | same |
| In-capsule driver | **OPA1662** (3–36 V single supply). THAT 1606 ruled out — ±4 V minimum split supply | `docs/capsule-board.md` |
| Capsule PCB | **Rev A designed**, 4-layer, 8.0 × 48 mm, ERC/DRC clean, gerbers exported. Not fabricated | same |
| Host platform | N100-class mini PC | `docs/hardware.md` |

---

## What is open

**Blocking M4:**

1. ~~**Parameter scaling.**~~ **All five measured, and confirmed on the wire.**
   - `delay_raw`: **solved**, integer samples at 48 kHz. Re-confirmed against
     the app's ms display on five channels to four decimal places.
   - `gain_raw`: **solved**, `gain_dbfs = gain_raw / 10 − 60`. Exact on all
     eight channels across raw values 433/470/480/500. Confirmed against the
     app display; not yet confirmed that the device honours its own display,
     which is a one-measurement job.
   - EQ `level`: **same encoding**, `level_raw` 600 = 0.0 dB. Confirmed at one
     point; the tune's 510–619 range maps to −9.0…+1.9 dB, which is the right
     shape for a real tune.
   - `bw`: **solved**, `octaves = (bw_raw + 5)/100`, displayed as Q via the
     standard peaking relation. Five points, Q 0.99–4.97. Typed Q values snap,
     rounding bandwidth up so Q never exceeds the request.

   **All four are measured and M0's scaling work is done.** Gain is confirmed
   end to end rather than only against the display: stepping OUT5 by a
   requested 6.00 dB moved the measured passband by 6.01 dB, inside a 0.11 dB
   run-to-run spread. Frequency likewise (450 Hz set, 449.4 Hz measured).
   ~~`bw` and EQ `level` are confirmed against the display only.~~ **Both
   closed by measurement 2026-08-09**: `level_raw` 720 produced +11.98/+11.99/
   +11.99 dB and 480 produced −12.02 dB; `bw_raw` 25/65/134 produced their
   requested half-gain widths to ±0.8 %. **Nothing in the parameter chain now
   rests on the vendor app's display.**
2. ~~**Discrete frequencies.**~~ **Closed 2026-08-08 by measurement.** A
   450 Hz crossover corner measures 449.4 Hz (−0.14 %); snapping to the nearest
   table entry would have put it at 420 Hz, +6.99 % away. The app also accepts
   1234 Hz typed by hand. `tuner.optimize.biquad` may fit frequency
   continuously; the discrete-frequency rewrite is not needed. See
   `docs/dsp408-protocol.md`.
3. ~~**Channel-to-chip mapping.**~~ **Closed 2026-08-11.** `0x37` drives
   outputs 1-4, `0x35` drives 5-8, measured on the ADAU control bus and
   falsified by a reverse-order re-run. `DeviceLimits`/`SimulatedDsp` now model
   two per-chip pools. What remains unmeasured is the *size* of each pool, and
   program space, which is not modelled at all.

4. ~~**Does a device EQ band realise an RBJ curve?**~~ **Answered 2026-08-09:
   yes, half-gain convention.** Measured on OUT1 at `bw_raw` 25 / 65 / 134 —
   half-gain error inside ±0.8 % across a 4.6× bandwidth span while the −3 dB
   reading sat pinned near −46 %, plus a cut that matched the boost to 1.1 %.
   `q_from_bw_raw` and `biquad.py` stand unchanged. Full results in
   `docs/dsp408-protocol.md`. The historical framing below is kept because the
   *reasoning* — that a wrong bandwidth convention looks like a mediocre
   optimizer rather than a wrong model — is the part worth reusing.

   Worth being precise about what this blocks: **not** the M4 orchestration,
   which does not touch the conversion. (This said "which is plumbing"; the
   orchestration owns the objective freeze, the per-session floor, the
   three-outcome logic, acoustic rollback verification and device-gain-aware
   safety. It is the hardest remaining code in the project, not glue.) What it determines is
   whether the loop *converges*. A wrong conversion would make every fit land
   off-target, the improvement invariant would catch it, and the run would roll
   back — safely, but repeatedly, and diagnosing that from inside a tuning run
   is far worse than knowing beforehand.

5. ~~**Preset recall and store opcodes.**~~ **Captured and implemented
   2026-08-09.** `user_id` on an `OUTPUT_CHANNEL` frame selects the slot: a
   store is a name write plus eight 296-byte record writes, and **a recall is
   eight READs** with no select opcode anywhere. There are six slots, not the
   fifteen the name list implied.

   Rollback is therefore no longer a write-back through the path whose failure
   it would be recovering from. `snapshot.store_as_preset` /
   `restore_from_preset` restore all eight channels in ~5 s, and a `.DDP` load
   gives a second path that fails differently.

   It also closed a hole in `txpolicy`, which permitted the recall frame
   because it looked like an ordinary read. **Reads are not inherently safe on
   this device.**

**Not blocking:**

- ~~`require_linear_path` false-positives on any filtered channel.~~ **Fixed
  2026-08-08** with a three-outcome result: tones whose response falls more than
  40 dB below the loudest are discarded, and fewer than two usable tones raises
  `IndeterminateLinearity` rather than passing or failing. The real OUT5 bench
  data now passes with two usable tones (`tests/test_qa.py`).

  **Residual, and tracked:** the discard is relative to the loudest tone, not to
  a measured noise floor, and the probe frequencies are still a fixed default
  rather than derived from the channel's crossover. A **narrow** channel — a
  sub, say — can still degrade to indeterminate. The upgrade is passband-aware
  tone placement; it is in the desk-cleanup list.
- ~~Which characteristic to write.~~ **Moot.** The app does not use GATT at
  all; it uses RFCOMM on SPP server channel 1. The BLE characteristics remain
  a documented dead end, and whether that path *also* works is untested and
  not on the critical path.
- `tuner.audio.devices` is still stubbed, including
  `verify_simultaneous_capture`. **It is an M1-*extension* gate for
  multichannel interfaces, not a gate on the single-channel Scarlett rig that
  validated M1** — the earlier wording called it "the M1 acceptance gate" while
  M1 was marked done, which was a genuine contradiction.
- ~~**REW golden tests do not exist**~~ — **built 2026-08-09**, and they
  earned their keep on the first run. 0.35 dB max / 0.09 rms over
  30 Hz—3.5 kHz (`tests/test_golden_rew.py`). Above that band the
  *reference* is the limit, not our engine: REW's own run-to-run scatter is
  0.370 dB rms against our 0.080.
- Microphone calibration rig (M2) untouched. It gates *calibration* of the
  capsule array, not its fabrication — the boards can be ordered and built
  before M2 exists. Note it **does** gate the capsule array and any absolute-SPL
  target, so "non-blocking" holds only for single-mic magnitude work.
- Interface build (`docs/measurement-interface.md`) — the 6-in/4-out box is
  still design-only and nothing is ordered. Deliberately off the critical path;
  the Scarlett rig stays authoritative until the new one reproduces its
  measurements. Stage 2 (XK-AUDIO-316-MC-AB dev board) remains the
  full-custom-vs-module decision point and has not been reached.

---

## Next session

**M0 is complete.** The rig is validated and the DSP fully characterised. Wiring, settings and every
measured figure are in `docs/hardware.md` ("Measured: DSP-408 on the bench").

### Connection protocol — follow this every session

1. **Select devices by name, host-API qualified** —
   `'Speakers (Scarlett Solo USB), Windows WASAPI'`. Never by index; MME
   renumbers when the Windows default output changes.

   **Use WASAPI, not MME** (measured 2026-08-09): identical curve to 0.236 dB,
   but 2-5x less run-to-run scatter from 250 Hz to 10 kHz. `bench_golden.py`
   defaults to it and takes `--host-api`; `bench_peq.py` and
   `bench_crossover.py` still name MME and should be moved when each next runs
   against hardware, **each with its own known-answer check**.
2. **Monitor knob hard at maximum, input gain hard at minimum.** Both verified
   on their end stops (2026-08-08). End stops are the only reproducible
   positions an analog knob has; confirm both before any session whose absolute
   levels will be compared against a previous one.
3. **Configure the DSP over USB-B, then unplug it before measuring.** The USB
   ground path adds a 100 Hz harmonic series 43 dB above the clean floor.
   Straight to a motherboard port — a hub stopped it enumerating at all.
   Unplugging USB is safe for the configuration: USB-B carries no power to the
   unit, so the live state survives. **Removing the wall wart does not.**
4. **A power cycle is safe; a preset recall is not.** Measured 2026-08-08:
   parameter writes are immediately non-volatile and come back byte-identical
   after pulling power. What destroys an edit is **recalling a preset**, which
   overwrites the working area. If a preset was loaded at any point, re-confirm
   the configuration before trusting anything measured after it — the capture
   would be a clean, plausible curve of the wrong tune. See "Persistence" in
   `docs/dsp408-protocol.md`.
5. Pause host audio. Parking the Windows default on another interface is fine
   *because* devices are selected by name.
6. Run the linearity check once per session; the quiet and signal checks are
   automatic.

### Operator inventory — answered 2026-08-08

Per "Ask the operator before reverse-engineering" in `CLAUDE.md`. All of these
are **operator statements**, the weakest evidence grade; recorded as such.

| Question | Answer | Consequence |
|---|---|---|
| How was `dspcartunebackups.DDP` produced? | **Read off the device** with the app's read function | Kills the "app quantizes on send" theory. The device *stores* 8619 Hz and 450 Hz. One confirming measurement outstanding. |
| Spare or donor DSP-408? | **No — one in-service unit** | Write posture unchanged: vendor app only, backup-and-verify, no undecoded bytes. BLE first-write stays deferred. |
| Physical access? | **Will open and probe** | Channel-to-chip mapping becomes a continuity trace instead of an inference. Unblocks the resource-budget model. |
| Test equipment? | **Multimeter.** No oscilloscope. Willing to buy an ST-Link and a logic analyzer | See "Equipment" below — one of these is worth buying now, one later, one not at all. |
| What is wired to each output? | **1–2 mid, 3–4 tweeter, 5–6 mid-woofer, 7–8 sub.** Nothing at all on the bench — the DSP comes out of the car and only the output under test is cabled | Records the channel inventory for in-car work; corroborates the tune's crossovers. Does *not* license raising any ceiling — see `hardware.md`. Also determines which outputs are even reachable on the bench. |
| Other saved tunes or presets? | *Still open* | Each is a free sample of the parameter space at zero risk. |

### Equipment: what to buy

(Answers above; recommendations here.)

- **ST-Link clone (~$3) — buy it.** Already the highest-payoff Tier-2 target in
  `dsp408-protocol.md`. An SWD dump of the STM32F103 could yield the parameter
  address map and the channel-to-chip grouping outright, and it is the cheapest
  item on the list by an order of magnitude. Readout protection may block it;
  that is a $3 gamble on a large payoff.
- **Logic analyzer (~$10 clone) — buy it *after* the case is open.** The I²C tap
  between the STM32 and the two ADAU1701s directly shows which chip receives
  which channel's parameters. It needs physical access to the DSP card, so it is
  only worth ordering once the teardown confirms the header is reachable.
- **Oscilloscope — do not buy one for this project.** The measurement rig is
  already a better instrument for everything in the audio band: two synchronous
  channels, ±0.35 dB, 0.07 dB level linearity, with deconvolution processing
  gain a scope cannot match. A scope earns its cost on fast digital edges and
  power-supply noise, neither of which is on the critical path here. Buy one
  because you want one, not because this project needs it.
- **The multimeter is the right tool for the teardown**, not for level
  measurement. Continuity tracing is exactly its job. For gain scaling, prefer
  the rig — many DMMs are average-responding and calibrated only near 50/60 Hz,
  so their AC accuracy at 1 kHz is unspecified, while the rig is characterized.

### What to do next

Re-cut 2026-08-10, after M4. **Everything that can be built without hardware
now is.** The numbering below is historical; the order of work is:

1. **First contact** (§3) — the only remaining item that needs the device.
2. **Desk cleanups** (§2) — small, and none of them block anything.
3. **Optional, and now cheap:** the ADAU parameter map. Output 1's gain lives
   at ADAU parameter `0x0810`; the same technique reads every other channel's
   addresses off captures we already have.

#### ~~1. M4 — the orchestration run~~ — **built 2026-08-10**

`tuner.orchestrate` is the run: `plan.py` freezes the inputs, `objective.py`
supplies the scalar and the freeze, `run.py` executes the stages, `isolate.py`
makes one driver audible and proves it, `rig.py` is the (never-run) adapter to
`capture_sweep`. Eighty-two tests, and `tools/tune_run.py rehearse` drives
all six outcomes against the fake device — accepted, rejected, indeterminate
by provenance, indeterminate by global state, aborted mid-run, and a rollback
that failed its acoustic check — plus the isolation and gang refusals, and a
run that stops before the write because no verdict could ever be reached, 41/41.

The stages, in the order the run enforces:

| Stage | Does | Refuses |
|---|---|---|
| `ARM` | Snapshot to disk, verify the digest, store the baseline to the scratch slot, confirm the store left the working area untouched, then arm writes | A preset store that changed any output record |
| `FLOOR` + `BASELINE` | N full measurement sets, each scored by the actual objective; spread is the floor, last set is the baseline | A floor from another session |
| `FIT` | Model-space only. Spatial power-average, level/shape split, `biquad.fit`, optional `delay.align`, `budget.account` | A configuration that does not fit the delay pool — before any write |
| `WRITE` | One pass. `compare_system` first, then `write_channel` per output, then a diff manifest | Global state that moved between arming and writing |
| `VERIFY` | Re-measure, re-check both fingerprints, score, `verify.verify` | An objective or plan that moved; global drift → indeterminate |
| `SETTLE` | Accept and disarm, or roll back via preset recall then block restore, then **re-measure** | A rollback whose re-measured score misses the baseline by more than the floor |

Five things worth carrying as method rather than as API:

- **The objective is fingerprinted, not merely documented as frozen.** A
  SHA-256 over the target's own points, the axis, the band, and every weight,
  taken at plan time and re-hashed immediately before the scores are computed.
  Re-weighting after seeing results changes the hash, and the run refuses to
  report the comparison rather than reporting it honestly-but-wrongly.
- **That check sits after the verification sweep, not at stage entry.** A test
  written to prove the freeze worked found that it did not: the check ran
  before the longest operation in the run, leaving the whole sweep as a
  window. That window is precisely when a person is sitting in front of the
  results so far, which is when an objective gets adjusted.
- **A tuning run requires `PeqPolicy.EXCLUSIVE`, and refuses to start
  otherwise.** Under `LEADING` a fit with fewer bands than the previous one
  leaves the surplus bands running. Every layer would still look correct — the
  writes succeed, the readbacks match, the fit is good — and the verdict would
  be about a system that was never configured as predicted.
- **A raised channel ceiling needs its basis in writing.** `DriverCeiling`
  takes a required free-text `basis` naming what is connected, which is what
  makes hard safety rule 4's "deliberate act" mean something more than a
  keyword argument. It goes into provenance, so a ceiling that turns out wrong
  is traceable to the claim that set it.
- **`None` is not `False` in the rollback report.** An abort can *be* the
  measurement path failing, in which case a re-measurement is not evidence of
  anything, so the abort path records "not checked" rather than claiming a
  verification it did not make.

**The objective is shape-only, and no report should imply otherwise.** Our
magnitude is dBFS, not dB SPL, so an absolute-level objective would be scoring
the interface's input gain. Channel gain is still set, from `level_offset_db`,
but its correctness is **not** evidenced by the verdict. Closing that needs M2.

Two constraints carried forward unchanged: `harman_in_car` raises by design
until real cited values are supplied, so a first run needs `tilted()` or
`from_points()`; and `align_delays` raises without a hardware loopback, which
the Scarlett Solo does not have. The run does **not** catch that raise — a run
that asked for alignment on a rig that cannot time it should stop, not quietly
report a magnitude-only tune as aligned.

##### Channel isolation — answered by the operator, 2026-08-10

**The manual method:** tune output 1 by muting 2—8 in the vendor app, confirm
visually that they read as muted, sweep. That is the right shape, and
`tuner.orchestrate.isolate.MuteIsolator` automates it. Two things about it
change once nobody is watching, and both are now enforced.

**A visual check of the app is a weaker claim than a readback of the device.**
Not a criticism of the manual method — with a human present, eyes plus ears is
fine belt and braces. But the app shows what it *believes*, and this project
has already caught it believing wrongly: in two captures it displayed a pair as
linked while `linkgroup_num` was stored as 0. So the isolator writes mute, then
**reads the enabled byte back from the device** on all eight and refuses if any
disagrees. One transaction each, and it is the device's answer rather than the
app's.

**Neither a visual check nor a readback is a measurement**, and rule 5 asks for
one. A readback proves the byte we wrote is the byte stored. It cannot see a
microphone picking up a path that never went through the DSP, a driver fed by
an output nobody is managing, or `enabled` not meaning on this unit what the
A/B said. So `prove_silence()` runs **once per session**: mute everything,
sweep, and require the capture to come back silent.

That proof is the existing `SilentPath` check inverted — a sweep that
*succeeds* is the failure. It costs one sweep, needs no new measurement code,
and is the only cheap thing that distinguishes "the mute bit is set" from "the
driver is quiet". `isolate()` refuses to run until it has passed.

Four consequences worth carrying:

- **A linked pair cannot be isolated from itself, and the isolator refuses
  rather than half-muting one.** The app keeps a pair consistent by writing
  both, so muting one half either mutes both or leaves the device disagreeing
  with the model. Unlink in the vendor app **before the baseline snapshot** —
  unlinking is a device state change, and the run would otherwise roll back
  into the linked state.
- **Isolation is not part of the tune, and the MISC block does not know
  that.** Gain, delay and the mute bit share one block, so a tune write carries
  whatever mute state the fit happened to see. The run restores isolation
  *before* the write stage and re-reads each channel's mute immediately before
  writing it. Without that, an accepted tune leaves seven channels muted in the
  car — with a clean verdict.
- **Every mute write is immediately non-volatile.** The read-modify-write skips
  a write that changes nothing, so moving isolation from output *n* to *n+1*
  costs **two** writes, not sixteen. Endurance figures for the part are still
  unknown and must not be invented; two-versus-sixteen is worth having without
  one.
- **The rollback re-measurement runs under the same isolation the baseline
  used**, then restores, then re-compares bytes. Otherwise it would be scoring
  one driver against all eight.

`NoIsolation(basis=...)` covers the bench case — one output wired, everything
else physically disconnected. It requires a written basis for the same reason
`DriverCeiling` does: it is a claim about the world that no code can check, so
somebody types the reason and it lands in provenance. It deliberately does
**not** sweep, because there is nothing a sweep could confirm and emitting a
stimulus to learn nothing is a stimulus that should not be emitted.

##### The setup token — the fourth member of that family, built 2026-08-12

`Provenance.setup_token` is the same shape again: a claim about the physical
world, typed by a person, recorded verbatim, unverifiable by any code. It says
the configuration is unchanged — microphone position, seat position, doors,
windows, HVAC, occupancy.

It exists because provenance previously gated **only** on temperature, which
is the weakest environmental term there is. Temperature moves the speed of
sound by about 0.17 %/K, which shifts the frequencies of multipath
cancellations; every one of the variables listed above moves the response
further, and none of them was checked. So an acoustic comparison could pass
while somebody had moved the microphone five centimetres — a sizeable fraction
of a wavelength at 3.5 kHz. **That is false confidence, which is worse than no
check.**

| | |
|---|---|
| Acoustic | **Required.** No token, no comparison, whatever the thermometer says |
| Electrical | Optional — but **binding once declared**, because a cable moved from OUT1 to OUT2 is a real change no other field records |
| Comparison | Trimmed at construction, then literal. Case folding or collapsing internal whitespace would be leniency, and leniency is what lets two different setups match |
| Refused where | `AcousticMeasurer.__post_init__`, before any sweep |

The asymmetry is deliberate: a token that changes when nothing moved costs a
refused comparison, and a token that stays when something moved is a false
verdict. Every default leans toward the first. What it cannot do is stop an
operator hardcoding one string forever — the same limit `DriverCeiling.basis`
has, and worth saying out loud rather than implying the check is stronger than
it is.

###### And the ordering fix, which generalises past the token

The first hardware loop armed, measured, fitted, wrote **eleven blocks**, and
only then found at VERIFY that no temperature had been supplied. Nothing was
wrong with the tune; the run could not say so, and *it could have known before
it changed anything*.

`Provenance.self_comparable()` makes that checkable. Comparing a provenance to
itself looks vacuous and is not — every pairwise term cancels, leaving only
what is structurally required. `TuneRun` runs it on the first measurement set
and stops there, before the fit, with nothing written. Rehearsal Stage 4
covers it, and asserts on *where* the run stopped rather than merely that it
failed: a run that reaches WRITE and rolls back passes any weaker check.

**A precondition evaluable at the start must not be discovered at the end** —
and on a device with no undo, "the end" is after the write.

##### ⚠ Outputs 7 and 8 are one loudspeaker, and the device's own flag does not say so

**Operator, 2026-08-10:** outputs 7 and 8 each drive a subwoofer, and **both
drivers share one ported box**. They are linked so their gains stay matched,
because mismatched drive in a shared enclosure is a mechanical problem, not a
tonal one: the box pressure is common to both cones, so the harder-driven
driver takes more than its share of the excursion while the other is
back-driven by air it is not generating force against. Below the port tuning
frequency, where excursion is already high and the cone is barely loaded, that
is where a driver fails.

Two consequences, in opposite directions.

**Measuring them together is correct, not a compromise.** Two drivers in one
enclosure, low-passed at 55 Hz, radiate as a single acoustic source at the
listening position. There is nothing to separate. The run should sweep the
pair once and correct it once.

**And the pair must never be corrected separately** — not gain, not delay, not
EQ. That is a hard constraint on the optimizer, on the same footing as the
resource budget.

###### The flag that is supposed to protect this is unreliable, and the corpus proves it

Checked across all 40 `.DDP` backups on 2026-08-10:

| | outputs 7 and 8 |
|---|---|
| gain | **identical in all 40 files** |
| low-pass | **55 Hz in all 40** |
| delay | **identical in all 40** |
| `linkgroup_num` | `1` in 26 files, **`0` in 14** |

The fourteen zeros are the `c1_hpf*` / `c1_lpf*` A/B series — the session where
the app was already known to have written unlinking and never written
re-linking. So the pair stayed matched by the operator's intent throughout,
while the **device's stored flag said unlinked**.

That is the hazard, and it lands squarely on us rather than on the vendor app.
`Dsp408Device.link_partners()` reads `linkgroup_num` from the device, exactly
as the rules require — and during those sessions it would have returned empty
for outputs 7 and 8. `modify_block_mirrored()` would then have written one
subwoofer and not the other, and `txpolicy.refuse_linked_channels` would not
have fired either, because the device says there is no link to refuse.

**Every safety net we have for this pair is keyed off a flag that reads zero
precisely when it matters.** Reading link state from the device rather than
from the app was the right call and remains it — the app is *less* reliable,
not more. The conclusion is narrower: **a device flag is evidence, and for a
constraint whose violation breaks hardware, evidence is not enough.**

###### ~~What that requires~~ — **built 2026-08-10**

**Gang membership is declared in the plan, not read from the device**, for the
same reason `DriverCeiling` and `NoIsolation` are: it is a fact about the
physical world that the device cannot be trusted to report. The device's
`linkgroup_num` is now a **cross-check** rather than a source.

`orchestrate.plan.Gang(outputs, basis, name)`. A gang of more than one needs a
basis, because that is the claim; a solo gang does not, because it is not a
claim about anything. Every source the objective weights that no gang covers
becomes a solo gang implicitly, so an eight-single-driver system declares
nothing and the run still has one shape to handle.

**Source and output stopped being the same word.** A *source* is one thing
measured once and corrected once, named by its lowest output. `plan.sources`
is `(0, 6)` for a system tuning output 1 and the subs; `plan.outputs` is
`(0, 6, 7)`. The distinction is load-bearing in both directions: the objective
scores two sources, and the delay pool is charged for three channels. The
objective's `output_weights` was renamed `source_weights` rather than left
alone — keys that sometimes stand for two outputs is the quiet mismatch this
project exists to avoid.

What the run does with a gang:

| | |
|---|---|
| **Sweep** | Members go audible together. Two drivers in one box are one acoustic source at the seat; there is nothing to separate |
| **Ceiling** | The **minimum** across members, so a gang is never louder than its most fragile driver would allow alone. `characterized` only if *every* member is |
| **Fit** | One fit per source, applied to every member |
| **Write** | Every member, then a **readback** check that they hold one tune |
| **Budget** | Per physical output — a two-driver gang spends its delay twice |

Four things worth carrying as method:

- **The agreement check is a readback, not a comparison of intent.** Writing
  the same config twice and assuming both landed is exactly the assumption the
  improvement invariant exists to distrust. A partial write, a refused frame
  or an off-by-one channel id all produce the mismatch this catches, and none
  of them is visible from the sending side.
- **It runs twice, and the two catch different things.** At `ARM` against the
  baseline, and again after the write. The first catches a mismatch the run
  did not cause — and refuses rather than levelling it, because levelling
  silently changes something nobody asked us to change and a rollback would
  put the mismatch back anyway. The honest options there are all the
  operator's.
- **`tuning_digest` had to learn what "the same tune" means.** The first
  version hashed all 31 EQ blocks verbatim and two untouched channels
  disagreed, because the vendor app seeds each channel's unused slots with its
  own default frequencies. Those bands sit at 0 dB and are inaudible whatever
  their frequency, so the digest now skips them. A check that fires on a
  difference nobody can hear is a check that gets deleted.
- **A declared gang lifts `txpolicy`'s linked-channel refusal, via
  `acknowledge_gang()`.** The refusal is right by default — writing one half
  of a pair leaves the device disagreeing with the model — and a caller that
  writes every member with the same values has not created that disagreement.
  Only the caller can know that, so it has to say so, and the isolator has
  already refused a gang that covers a link group only in part.

The two directions of gang-versus-device disagreement get different treatment,
which is the whole point of the cross-check:

| Device says | Plan says | Response |
|---|---|---|
| linked | no gang | **Hard stop.** The pair may share an enclosure and this code cannot tell |
| linked | gang covers it partly | **Hard stop.** Measured together, written apart, the worst of both |
| linked | gang covers it exactly | Proceed, and acknowledge to `txpolicy` |
| unlinked | gang declared | **Warn and proceed.** The run is safer than the device here: it writes both members identically because the gang says so. What the warning records is that the vendor app could move one alone in this state |

That last row is the observed state of outputs 7 and 8 in 14 of the 40 `.DDP`
backups, and the warning lands in the run report.

###### What M4 still does not have

- **`AcousticMeasurer` reached hardware on 2026-08-12, through a cable.** It
  drove the closed loop's sweeps on the electrical bench rig. **It has still
  never met a microphone**, so nothing acoustic about it is exercised: no room,
  no propagation, no `prove_silence` that could mean anything.
- ~~**The scratch slot is still undecided.**~~ **Slot 6, confirmed by the
  operator 2026-08-10**: "I don't actually utilize it anymore." It reads
  `basssss++++` in the capture, so it holds a real tune of theirs and storing
  over it destroys it — which is the operator's call, made knowingly, and
  recorded here as the claim it is.

  **The plan still refuses to default to it.** A designation recorded in a
  document is not the same as a value the code will pick on its own, and the
  reason for requiring `scratch_slot` and `scratch_slot_confirmed_by` at
  construction has not changed: the next unit, or the next car, has a
  different answer.

  Cheap and reversible, if the contents are ever wanted: recall 6, save a
  `.DDP`, recall 4. Two reads, no writes. **It is only safe while the working
  area holds a stored preset** — it does today, because preset 4 was just
  recalled — since a recall overwrites the working area with no undo. After
  the first tuning run that is no longer true.
- **The `TxPolicy` a real run needs is wider than the bring-up one.** Isolation
  touches all eight channels, so `BlastRadius(max_channels=1)` will not do,
  `allow_presets` must be on for the baseline store, and a ganged run
  acknowledges its linked outputs (the run does that itself, at ARM). All
  deliberate widenings, set per run rather than defaulted.

#### 2. Desk cleanups. Also no hardware, and all small.

- **Move `bench_peq.py` and `bench_crossover.py` to WASAPI.** `bench_golden.py`
  already defaults to it. Each needs **its own known-answer check** when it next
  runs against hardware — a host-API change is a change to the measurement
  chain, and that is the rule that caught the `_combine_passes` question.
- **Rename `gain_raw`/`delay_raw`** to engineering units. All the mappings are
  measured; the conservative naming has outlived its reason. Touches
  `protocol.py`, `sim.py` and their tests.
- **Passband-aware linearity tones.** `require_linear_path` can still return
  indeterminate on a narrow channel because the default tones fall in its
  stopband. The three-outcome fix is done; picking tones from the channel's own
  crossover is not.
- **`EqBandType` is mapped but unreachable through `ChannelConfig`**, which has
  no shelf concept. The backend refuses shelf bands rather than mismodelling
  them; whether the optimizer should ever *use* a shelf is a design question, not
  a protocol one.

#### 3. ~~First contact~~ — **SUCCEEDED 2026-08-11.**

**The backend has transmitted and read correctly. M3's last gate is closed.**

    31 requests, clean=True        firmware MYDW-AV1.06

Read-only throughout, `allow_writes=False`. All eight 296-byte channel records
read back, every reply's header echo exact, zero framing resyncs. First
snapshot of the real device is at `snapshots/2026-08-11_first-contact.json`,
digest `44a18095c4a4...`.

##### The first attempt failed, and why is the finding

The same command against the same device returned **total silence** — link up
in 0.76 s, our frame byte-identical to the vendor app's first frame, and not
one byte back over 12 s.

**The device had an active USB control session at the time.** The Windows
vendor app was connected over USB-B, and the DSP-408 evidently arbitrates: it
accepts the RFCOMM link at the link layer while its protocol handler stays
bound to USB. Unplug USB, and the identical command works first time.

Two things follow.

- **The SDP puzzle is resolved, and by a different fact.** *The successful
  connection* settles it: channel 1 is reachable without an advertised SPP
  record, full stop. *The USB session* explains the silence. Keep them as two
  findings — they would be falsified by different experiments, and merging them
  makes the arbitration hypothesis look like it carries evidence it does not.
- **The operator caught it, not the code.** The test was run without confirming
  what else was attached to the device — exactly the failure the "device state
  is cheap to verify, so verify it rather than inferring it" rule exists to
  prevent. One question would have saved a wrong conclusion; the conclusion had
  already been written into this file as "hypotheses for next session" before
  the operator volunteered the USB session unprompted.

**Operational rule, now measured: only one control transport at a time.** Any
run must confirm USB is detached before opening the Bluetooth socket, and the
orchestration should check it rather than trust it.

##### The readback independently confirms the whole I2C session

Two measurement routes, sharing no code, agreeing exactly. The RFCOMM readback
of `gain_raw` against the app values recorded during the logic-analyser work:

| out | chip | `gain_raw` | RFCOMM dB | app value | app - 60 |
|---|---|---|---|---|---|
| 1 | `0x37` | 500 | -10.0 | 50 | -10 |
| 2 | `0x37` | 440 | -16.0 | 44 | -16 |
| 3 | `0x37` | 480 | -12.0 | 48 | -12 |
| 4 | `0x37` | 480 | -12.0 | 48 | -12 |
| 5 | `0x35` | 480 | -12.0 | 48 | -12 |
| 6 | `0x35` | 480 | -12.0 | 48 | -12 |
| 7 | `0x35` | 370 | -23.0 | 37 | -23 |
| 8 | `0x35` | 370 | -23.0 | 37 | -23 |

All eight agree. That closes `dB = displayed - 60` and, with the ADAU
coefficients decoding to the same figures in 5.23 fixed point, gives three
independent paths to the same numbers: the wire protocol, the app display, and
the DSP's own coefficient memory.

##### And the system it describes

    OUT1  -10.0 dB  delay 144   450 Hz - 3.5 kHz    midrange     chip 0x37
    OUT2  -16.0 dB  delay 156   450 Hz - 3.5 kHz    midrange     chip 0x37
    OUT3  -12.0 dB  delay   0   3.5 kHz - 20 kHz    tweeter      chip 0x37
    OUT4  -12.0 dB  delay  12   3.5 kHz - 20 kHz    tweeter      chip 0x37
    OUT5  -12.0 dB  delay  36    55 Hz - 450 Hz     mid-bass     chip 0x35
    OUT6  -12.0 dB  delay  60    55 Hz - 450 Hz     mid-bass     chip 0x35
    OUT7  -23.0 dB  delay  66    20 Hz - 55 Hz      subwoofer    chip 0x35
    OUT8  -23.0 dB  delay  66    20 Hz - 55 Hz      subwoofer    chip 0x35

The chip split is upper half / lower half. **Outputs 7 and 8 are identical in
gain, delay and crossover** — the shared-enclosure gang, matched, as
`orchestrate.plan.Gang` requires.

**Note for rule 4 and for any tune's high end:** outputs 3 and 4 are tweeters
crossed at 3.5 kHz, which is exactly where independent validation of the
measurement engine stops. Their ceilings stay at the conservative default until
a `DriverCeiling` with a written basis says otherwise.

##### What is still untested

~~Everything that writes.~~ **Stages 2, 4 and 5 ran on 2026-08-11 and all
passed** (see the top of this file). Our code has changed the device and put it
back, verified by re-reading.

What is still untested, stated narrowly so it does not erode again: **more than
one block in a run, more than one channel in a run, and preset recall as a
restore path.** A fit writes tens of blocks across eight channels; Stage 5 wrote
one block on one channel, twice. The blast-radius caps and `modify_block_mirrored`
have never run on silicon, and neither has a gang readback.

#### ~~4. Bench session~~ — **done 2026-08-09**

Sixteen `.DDP` A/Bs, two HCI captures, a REW comparison and a host-API
experiment. Results are in the table at the top of this file; the run sheet
[next-bench-session.md](next-bench-session.md) is now a record rather than a
plan. Three things worth carrying as method rather than as findings:

- **Three fields that "read 0 everywhere" were not opaque, they were
  unexercised.** `h_filter`, `h_level` and `EqBand.type` had no variation in the
  corpus because the operator had only ever used Linkwitz-Riley, 12/24 dB per
  octave, and no shelves. No further analysis of the backups could have produced
  the mappings; sixteen deliberate A/Bs took minutes. **Check whether the
  evidence is exhausted before concluding a field is.**
- **Decoding a field can create an obligation, not just an ability.** Mapping
  `EqBand.type` *added* a refusal, because writing peaking parameters into a
  shelf would produce a device running a filter nobody modelled — with a
  successful write, a matching readback and a plausible fit to hide it.
- **An independent check earns its keep by failing.** The REW golden found that
  MME was most of the HF artifact, that the reference was noisier than the engine
  it was checking, and that a neat windowing hypothesis explaining the corner
  disagreement was wrong.

#### 5. ~~M3 — the RFCOMM backend~~ — **built 2026-08-09; first contact 2026-08-11**

The whole stack exists and is replayed against `captures/btsnoop_hci.log`:
`framing`, `txpolicy`, `transport`, `fake_device`, `session`, `device`,
`snapshot` and `Dsp408Spp`. Our connect ritual and all 21 captured writes
reproduce **byte for byte**, and `tools/dsp408_probe.py rehearse` runs the whole
bring-up script with every abort path (29/29).

What remains is the part no amount of code can do: **connecting to the real
device.** ~~The two hardware transports have never opened a socket.~~
**`RfcommSocketTransport` opened one on 2026-08-11 and read the whole device;
`SerialPortTransport` still has not.** The staged
bring-up in §6 below is the procedure for that, and Stage 4's first write is a
no-op by design.

What was known rather than assumed when it was built:

- **Transport: classic Bluetooth RFCOMM (SPP), server channel 1.** A serial
  socket, not a GATT client. `bleak` is not a dependency.
- **Every link write is 20 bytes, zero-padded**; the reader must resync on the
  preamble and tolerate runs of `0x00`.
- **Strict lock-step, one outstanding request.** Replies echo the request header
  bit-for-bit — match on that tuple, not on ordering.
- **A write carries the whole 8-byte block**, so read-modify-write is mandatory.
- **`DataID 119` reads a whole 296-byte channel** — the snapshot primitive, and
  already validated against the vendor app's own export.
- **No commit step and no undo.** The preset opcode is now known (2026-08-09).

Planned modules: `framing.py`, `txpolicy.py`, `transport.py`, `session.py`,
`fake_device.py`, `device.py`, `snapshot.py`, plus `ddp.serialize`/`splice_outputs`.

Three design decisions worth carrying forward verbatim:

- **Invert the destructive-opcode guard into an allow-list.** The current
  blacklist is provably inert — `DataType 9 / ChannelID 95-99` had 0 hits in the
  whole capture, so it would never fire in a normal session. An allow-list
  validated against the capture is the real defence, and it is what protects
  against bricking.
- **Specify the error contract before writing any code.** `0x52` has never been
  observed; without a written contract the handler meets its first error during
  a write. Halt and poison on `0x52`, never retry it; one identical-bytes retry
  on timeout only.
- **Snapshot stores the 296-byte records verbatim**, never as decoded fields.
  Blocks 34/35 are undecoded and several encodings are unknown.

Still true: the only unit is in service, so the first write happens on the bench
with nothing wired to the outputs, and it is a **no-op** — a payload byte-
identical to what is already there, followed by a whole-record readback.

> #### ⚠ Stage 4 was unreachable through the sanctioned write path
>
> Found 2026-08-11 while building the bench tooling. `Dsp408Device.write_block`
> returns `False` **without transmitting** when the device already holds the
> payload — correct for a restore, where fewest writes is fewest risks and
> idempotence makes a partial failure safely re-runnable. It also made the
> bring-up ladder's safest rung impossible to climb: there was no way to send a
> no-op, so the first write to hardware would necessarily have been a real one.
>
> The ladder had been specified for months and the code contradicted it,
> silently, in a method whose behaviour is right on its own terms. **A staged
> plan is not enforced by being written down**; each rung needs a path that
> reaches it, and the way this surfaced was building the tool rather than
> re-reading the plan.
>
> Now `Dsp408Device.rewrite_block_unchanged`, run by
> `dsp408_probe noop-write --snapshot-out … --apply`. A **dedicated method, not
> a `force=True` flag**: it reads the block live immediately before sending it,
> so the payload cannot differ from the device's own state by construction
> rather than by the caller having checked. A flag would give the same effect
> and a weaker guarantee, and would be reachable by someone who only wanted
> idempotence.
>
> **What Stage 4 proves, and what it cannot.** It proves the transport carries
> a fragmented multi-chunk write over live RFCOMM, that the device answers
> `0x51` under our pacing, and that the 296-byte record and all seven other
> channels are unmoved afterwards. It **cannot** prove the bytes were stored —
> they were already there, so a device that silently discarded the write looks
> identical. `tests/test_device.py::TestNoOpWrite` asserts that limitation
> rather than only documenting it. It is why Stage 5 is a separate rung.
>
> One consequence worth knowing before reading a journal: a no-op entry has
> `before == after`, so `reconcile()` can never classify it `NOT_LANDED`. For a
> no-op, landed and not-landed are the same readback.
>
> The restore point is captured **inside the `noop-write` invocation** rather
> than named on the command line. A snapshot from an earlier session is
> evidence about an earlier session.

> #### ✅ Stage 5, the same evening: a real write and a verified rollback
>
> `gain_raw` 500 → 490 on OUT1, readback confirmed, rolled back in 2.7 s, then
> the whole device verified byte-identical to the first-contact snapshot from
> hours earlier. Journal:
>
>     before 0100 f401 9000 0001   after 0100 ea01 9000 0001   "gain 500->490"
>     before 0100 ea01 9000 0001   after 0100 f401 9000 0001   "stage 5 rollback"
>
> **The transition was chosen because the capture contains it literally.** The
> vendor app sent exactly this write while the operator dragged the gain
> slider, so the first state-changing write this project made was a byte
> sequence the device had already accepted from software it trusts. That is a
> materially stronger position than "a small change we reasoned was safe", and
> it is only available for values the capture happens to hold — which is an
> argument for choosing bring-up steps *from the evidence* rather than for
> convenience.
>
> **`stage5` is not a set-a-parameter command, deliberately.** It is hard-wired
> to that one transition and refuses any other starting `gain_raw`, exiting 2
> **before arming** rather than merely before transmitting. The general tool
> would have been easier and would have handed this project the ability to
> write arbitrary bytes to arbitrary blocks before it had ever changed one on
> purpose. Same reasoning as `rewrite_block_unchanged` being a method rather
> than a `force=True` flag: narrow the surface until the behaviour is proven.
>
> **What it did not prove.** One block, one channel, twice. A fit writes tens
> of blocks across eight channels, the blast-radius caps have never been hit on
> hardware, `modify_block_mirrored` has never run on silicon, and no gang
> readback has ever confirmed that outputs 7/8 hold one tune. Stage 6 is that.
>
> The neighbouring-bytes check is the one worth keeping in any future write
> test: the app's gain writes carry mute, polarity, delay, `eq_mode` and
> `spk_type` in the same eight bytes, and a backend that reverted any of them
> would produce a device the optimizer never modelled — with a successful
> write and a matching gain readback to hide it.

#### ~~6. Channel-to-chip mapping~~ — **settled 2026-08-11**

    ADAU at I2C 0x37  ->  outputs 1, 2, 3, 4
    ADAU at I2C 0x35  ->  outputs 5, 6, 7, 8

Logic analyser on the ADAU control bus, which turned out to be the unpopulated
**2x5 header: pin 10 = SCL, pin 9 = SDA.** Every output stepped one gain click,
and the chip that received the write recorded. Full account in
`docs/dsp408-protocol.md`.

`DeviceLimits` now carries `output_chip`, `SimulatedDsp` enforces per-chip
pools, and `optimize.budget` reports usage per chip. **The grouping is
measured; the pool sizes are not** — `delay_samples_per_chip` is 1024, which
preserves the old placeholder's device total rather than doubling it, and
`measured` is still False.

Four things from the session worth carrying as method:

- **A negative result from an unvalidated probe is not a result.** Seven header
  pins read dead flat and got written up as "the header is eliminated". They
  had been soldered to the underside of pads that are top-side only, so they
  were connected to nothing. The rig's touch test had validated *one* channel's
  path and the other seven were trusted on no evidence. Silence from an
  unverified probe means nothing at all.
- **Falsify an attribution that lands where you expected.** Outputs 3-6 shared
  the same gain value, so their chip assignment rested on the order they were
  edited in — and the answer came out exactly as the long-standing guess
  predicted. Re-running them in reverse order, and getting the same
  channel-to-chip assignment in the opposite sequence position, is what made it
  a measurement.
- **Sample rate is not a free parameter on a marginal USB link.** A survey that
  only needs "which pins move" was run at 16 MS/s across 8 channels — 128
  Mbit/s into a USB 2.0 device on a flaky hub — and dropped. Two MS/s, the same
  load as every capture that had worked, answered the same question.
- **The EEPROM write is the non-volatility rule, visible.** One byte committed
  per slider click. "Every write is immediately permanent" was inferred from a
  power-cycle test; now it has a mechanism.

**What is still unmeasured about the chips:** the size of each delay pool, and
program space, which is not modelled at all.

#### 7. ~~`tuner.optimize.target` — the rest of M4~~ — **done 2026-08-09**

`target`, `delay` and `budget.normalize_delays` are all implemented and tested
analytically. ~~What remains for M4 is the **orchestration**.~~ **The
orchestration was built on 2026-08-10** — see §1 above, which supersedes this
paragraph.

Two caveats carried forward: `harman_in_car` raises by design until real cited
values are supplied, so a run needs `tilted()` or `from_points()` for now; and
`delay` cannot be validated against hardware until an interface with a loopback
exists, since the Scarlett Solo has none.

Note also that `DeviceLimits` models no program space at all, and
`ChannelConfig` cannot express polarity, `spk_type`, mix, dynamics, name, link
group or bands 11-30 — which is why `Dsp408Spp` treats it as a view and carries
the rest through.

#### 8. Closed 2026-08-09: the HF artifact

Kept as a record rather than a task. It sat in this file as "still unexplained" for the whole project.

- **The HF artifact — still unexplained.** Multi-dB narrowband outliers above
  4 kHz survive median-of-3.

  `_combine_passes` medianing real and imaginary parts independently was the
  leading suspect and **has been fixed** (2026-08-09), but measurement cleared
  it as the cause: at the residual misalignment real captures actually see
  (0.05–0.11 samples, the parabolic peak estimator is biased), the error was
  only 0.1–0.36 dB at 20 kHz. Real, worth fixing — it reached 7.6 dB at 0.4
  samples residual — but an order of magnitude too small to be the artifact.
  **Look elsewhere.** MME dropouts and the interface itself are the remaining
  candidates.

  **Quantified and localised 2026-08-09, by the REW golden.** Two of our own
  runs, minutes apart on an unchanged electrical path, differ by up to **7 dB**
  between 10 and 18 kHz — *more* than either differs from REW. So it is a
  repeatability failure in our capture, not a systematic error in our analysis,
  and it is not REW's.

  | Condition | Our run-to-run scatter at ~14 kHz |
  |---|---|
  | Flat channel, strong HF signal (D1) | ~0.4 dB |
  | Low-passed at 3.5 kHz, 50 dB down at 14 kHz | **7 dB** |

  **It scales with how far down the received signal is, not with frequency.**
  That is the signature of dropouts or capture-path noise, and it rules out the
  analysis path — the maths does not know how loud the signal was.

  **Largely solved the same day: it was MME.** Switching host API to WASAPI,
  same DUT and level, back to back:

  | Band | MME | **WASAPI** | WDM-KS |
  |---|---|---|---|
  | 250–1000 Hz | 0.515 | **0.231** | 0.267 |
  | 3500–5000 | 0.317 | **0.091** | 0.251 |
  | 5000–7000 | 1.772 | **0.384** | 0.352 |
  | 7000–10000 | 1.418 | **0.792** | 1.504 |

  2–5× better from 250 Hz to 10 kHz, and 4.6× at 5–7 kHz where the signal sits
  at a comfortable −33 dBFS — so the artifact was never purely a
  signal-to-noise story. **Known-answer check licenses the switch:** WASAPI
  against MME agrees to 0.236 dB max, 0.058 dB rms, so historical MME
  measurements stay comparable, inside the 0.39 dB floor.

  **And the rest of it was never ours.** On a flat channel under WASAPI, our
  two runs agree to **0.395 dB max / 0.080 dB rms across 30 Hz–18 kHz** — there
  is no residual HF artifact in our engine to explain. What looked like one in
  the golden comparison was **REW's** scatter: its own two runs differ by
  3.311 dB / 0.370 dB rms, 4.6× worse than ours.

  So the artifact had two causes and neither was the analysis: MME below
  10 kHz, and the reference above it.

  `tools/bench_golden.py` now defaults to WASAPI and takes `--host-api`.
  `bench_peq.py` and `bench_crossover.py` still name MME; move them over the
  next time each runs against hardware, **each with its own known-answer
  check** — a host-API change is a change to the measurement chain.
- ~~**Cross-rig check — owed now, not hypothetical.**~~ **Paid 2026-08-09.**
  `_combine_passes` changed that day, which is a change underneath every
  measurement this project has made, so the crossover known-answer test was
  re-run on OUT5 over the original **200–1600 Hz at 400 points** window.

  | | Old combiner, 08-08 | New combiner, 08-09 | Δ |
  |---|---|---|---|
  | Corner | 449.4 Hz | **450.1 Hz** | 0.16 % |
  | rms residual | 0.241 dB | **0.247 dB** | 0.006 dB |

  **Every figure measured under the old combiner stands.** Details and the two
  incidental findings in `docs/next-bench-session.md` § D0.

  The window mattered and nearly went wrong: this entry originally said
  `--fit 150 2000`, but the 449.4 Hz figure came from 200–1600 and the same
  corner over 200–2000 reads 447.0 Hz. Running the recorded command would have
  measured the fit window and blamed the combiner for a 2.4 Hz shift.

#### Settled at the bench, 2026-08-09

| Question | Answer |
|---|---|
| RBJ filter shape | Half-gain convention, ±0.8 % across a 4.6× bandwidth span, symmetric |
| `fc` at 25 Hz | Continuous — 25/27 Hz fit to 24.9/26.9, exactly 2.0 Hz apart |
| Preset store / recall | `user_id` selects the slot; **a recall is eight READs**, no select opcode |
| Preset slot count | **Six**, not fifteen — 7–15 are a stale buffer |
| Mute on the wire | Ordinary `dt4/id31` block write, byte 0 |
| Master volume | `dt9/ch5/id0` byte 0 — global, one write moves every channel |
| `.DDP` load | Reaches the device; USB-only, no Android import |
| `DataType 3` | Unreachable from the Android app — that is why it never appeared |
| **Link mirroring** | **The app mirrors, the device does not.** Six writes linked, three unlinked, same three actions |
| **Clean disconnect** | **There is none.** Polling stops mid-stream; no goodbye frame |
| Measurement engine vs REW | 0.35 dB max / 0.09 rms over 30 Hz–3.5 kHz |
| HF artifact | MME below 10 kHz; REW's own scatter above it. Ours: 0.080 dB rms to 18 kHz |

**Still open after all that:** blocks 34/35, and ~~the channel-to-chip mapping~~ (**settled 2026-08-11**; this line is kept as it was written, annotated rather than rewritten, per the convention below).

This paragraph listed the crossover selectors and `EqBand.type` as open for
about an hour, until the A/Bs it suggested were actually run. Both are mapped
— see the table at the top of this file. Left visible rather than deleted
because the sequence is the point: the paragraph was correct when written,
wrong by the end of the session, and a reader who trusted it would have gone
looking for evidence that already existed.

#### Done this session

**Measured on the device:** all five parameter scalings; frequency quantization
disproved; the persistence and undo model; the EQ control ladder (bypass vs
reset vs restore); OUT7/8 explained by channel linking; the output-stage
topology from the teardown; the transport corrected to RFCOMM.

**Built:** `tuner.dsp.ddp`, `tuner.dsp.btsnoop`, `tuner.optimize.biquad`,
`tools/ddp_dump.py`, `tools/bench_crossover.py`, `tools/btsnoop_extract.py`,
`tests/test_golden_frames.py` and its fixture.

**Fixed:** `require_linear_path` given a third outcome and stopped
false-positiving on filtered channels; `ddp.diff` stopped hiding changes in
undecoded output blocks.

**Corrected in the docs:** transport (BLE → RFCOMM); MCU part (ST → Geehy);
EEPROM presence; frequency quantization; the naming rule on `gain_raw`; the
optimization-time estimate, now measured.

#### Done in the review pass (2026-08-09, later)

**Measured from the existing capture, no hardware:** the whole session layer —
20-byte zero-padded chunking, strict lock-step, the ack model, bit-exact header
echo, latency and pacing distributions, the 31-transaction connect ritual, the
`DataID 119` bulk read, and the absence of any preset opcode, `DataType 3`
frame, `0x52` reply or destructive opcode.

**Found:** the bulk record agrees with the vendor app's `.DDP` export byte for
byte (2368 bytes, two paths, no shared code); blocks 34/35 contradict
`OutputBlock.MIX_IN_9_16` and are now excluded from every write path;
`bluetooth_device_id` is 4 on the wire against our default of 0.

**Fixed:** `rfcomm_streams` folded RFCOMM's control channel into the data
stream, shifting every byte offset by 18 — invisible to frame recovery, fatal to
a replay oracle. `group_delay_samples` was reachable without a timing reference.
`IncomparableProvenance`'s raise branch had no test.

**Renamed:** `dsp408_ble.py` → `dsp408_spp.py`, BLE constants kept as a fenced
dead end.

**Corrected in the docs:** the REW validation claim (the goldens never existed);
the M1 acceptance-gate contradiction; the "linked pair moves from one slider
drag" line, which was a hypothesis written as a measurement.

#### Also done 2026-08-09 (the build pass)

**Built — the whole M3 stack, offline:** `framing`, `txpolicy`, `transport`,
`fake_device`, `session`, `device`, `snapshot`, `Dsp408Spp`, plus
`ddp.serialize`/`splice_outputs`. Connect ritual and all 21 captured writes
reproduce byte for byte against the capture.

**Built — the rest of M4's parts:** `optimize.target` (curves and the
level/shape split), `optimize.delay` (arrival-time alignment with a weighted
multi-seat compromise), `budget.normalize_delays`.

**Built — tooling:** `tools/dsp408_probe.py` (enumerate / snapshot / verify /
diff / restore / reconcile / `idle` / `noop-write`, plus `rehearse`, which runs
the whole bring-up script against the fake including every abort path, 29/29) and
`tools/bench_peq.py` (the D1 measurement, with a differential mode).

**Fixed in the measurement engine:** `_combine_passes` medianed the real and
imaginary parts of the spectra independently, which is not a complex median and
not rotation invariant. Replaced with median magnitude and the phase of the
coherent sum. **It had no direct tests at all** before this — the most
load-bearing routine in the engine, and nothing exercised it. It has 13 now,
including one asserting the old version fails them.

**Three things measured rather than assumed while building:**

- **+6 dB is a null test for the bandwidth convention.** Half-gain points sit at
  `G/2` and −3 dB points at `G−3`; they are equal at exactly 6 dB. An earlier
  draft of the run sheet specified +6 dB and would have answered nothing.
- **A differential sweep cancels the speaker, room, mic and interface**,
  recovering the filter to 1 part in 10⁴ through a hostile synthetic system,
  where a single sweep gets Q wrong by >5%.
- **Cross-correlating one driver against another is the wrong instrument** for
  time alignment — a tweeter and a mid-woofer share no passband, so the peak is
  built from stopband leakage. `arrival_samples` against the loopback avoids it.

**Settled by a two-minute operator A/B:** byte 0 of the MISC block is
`enabled` (1 = on), not `mute`. One byte changed in the whole file, `gain_raw`
untouched. The field had been deliberately left un-renamed in `ddp.py` for
months on the strength of a 111-of-112 survey — declining to guess was right,
and the guess it declined to make turned out to be correct. What waiting bought
was that nothing was written on the strength of it.

**Two claims of mine that measurement corrected**, recorded because the pattern
matters more than the instances: the coordinate-wise median defect is real but
is **not** the HF artifact (alignment suppresses it to ~0.1–0.36 dB, an order of
magnitude too small), and my first replacement for it measured *worse* than what
it replaced. Both were caught by measuring rather than reasoning.

Test count went 237 → 577 → 1058 → 1159 → 1241 → 1258 → 1292 → 1294 → 1297 → 1316 → 1327 → 1334 → 1347 → 1354 → 1365 → 1371 → 1388 → 1392 → 1396 → 1408 → 1454 → **1508**.

#### Standing rules for bench sessions

- **Save a backup before and after every change.** The diff loop is the cheapest
  instrument in the project and has already caught two changes nobody noticed
  making.
- **Never overwrite `dspcartunebackups.DDP`** — it is the only record of the
  original OUT1 tune and is pinned by tests. New saves get new names.
- **Never save a backup with EQ bypass engaged.** The file will be missing every
  EQ gain and it is not detectable afterwards.
- Readback exists (`READ_CMD`), so write→read→compare verification is available
  once M3 can talk to the device.

## Paused: the PCB build task (capsule board)

**Set down 2026-08-07.** Design is complete and verified; nothing is ordered.
Full design rationale, cited datasheet figures, bring-up order and open
questions are in **[capsule-board.md](capsule-board.md)** — that document is the
deliverable, this section is only the resume point.

**Where it stands.** KiCad project at `hardware/capsule-board/`. Rev A schematic
and 4-layer layout, **ERC 0 / DRC 0 / 0 unconnected**, 25 parts, 164 tracks and
vias, ground planes filled. Fabrication outputs (4-layer gerbers, separate
PTH/NPTH Excellon drill, board + schematic PDFs, BOM) are in
`hardware/capsule-board/fab/`. Library paths are `${KIPRJMOD}`-relative, so the
project moves with the repo.

**Nothing was left half-finished.** The design is at a clean stopping point; the
next step is a decision, not a repair.

### Resume here

1. **Review the schematic.** Open the `.kicad_pro`, not the `.kicad_sch` — the
   project-local `sym-lib-table` carries the custom `OPA1662` symbol, and
   opening the sheet directly shows it as missing. Nets are wired with labels
   rather than drawn wires, and reference designators are on **F.Fab**, not
   silkscreen (no room on an 8 mm board).
2. **Settle the gain split.** This board is unity-gain per phase (×2
   differential); the ~4 dB that `measurement-interface.md` computes is left at
   the interface end deliberately. Reversing that is a resistor change to
   R7/R8, not a redesign — but decide before ordering.
3. **Resolve the mechanical envelope.** The capsule is Ø9.7 mm, so the body
   needs ~10 mm ID / 12 mm OD. The 8–10 mm tube in the interface BOM will not
   fit, and no tube has been sourced.
4. **Check fab capability** before ordering: 0.25 mm copper-to-edge, 0.3 mm
   drill, 4-layer. Then order 5 boards + stencil and build two — the second is
   the control that separates an assembly fault from a design error.

### Do not re-derive these

- **THAT 1606 is eliminated**, permanently: ±18 V to ±4 V, a split-supply part
  needing 8 V minimum, against a single 5 V rail down the mic cable.
- **Output isolation resistors (R6/R9) are mandatory**, not stylistic. The
  OPA1662 drives 200 pF; 5 m of shielded cable exceeds that.
- **R5 = 2k2 is the capsule datasheet's recommended load**, not a guess.
- **4 layers is not gold-plating.** Two layers leaves ~3 usable channels on an
  8 mm board against 6 nets needing full-length runs; the only 2-layer routing
  cuts the ground plane under a high-impedance electret front end.

### Unverified by measurement

Everything. The operating point is calculated from datasheet typicals:
V<sub>BIAS</sub> ≈ 4.25 V, capsule drain ≈ 3.15 V at TP3. **TP3 is the first
thing to check on a built board**, and R5 is the adjustment if it reads near
either rail.

### Tooling notes

The KiCad MCP server's Python worker died mid-session and does not respawn — a
modal wxWidgets assertion (`PCB_VIA::GetWidth called without a layer argument`)
blocks it, and dismissing that dialog with "Yes" terminates the process. Restart
the MCP session before relying on those tools. The authoritative checks were run
outside the MCP with `kicad-cli` and KiCad's own `pcbnew`, which is the more
trustworthy route regardless; note `kicad-cli` does **not** refill zones, so
fill with `pcbnew` before believing a connectivity result.

---

## Environment

**Ghidra** at `./ghidra_12.1.2_PUBLIC` (git-ignored). **JDK 21** at
`C:\Program Files\Java\jdk-21.0.11` — not on the bash PATH; export `JAVA_HOME`.

**Phone** (Galaxy S10+, Android 12) over **wireless ADB** — USB never
enumerated, likely a charge-only cable. Re-pair each session: Developer
options → Wireless debugging → pair with code, then `adb pair` and
`adb connect`. Platform-tools and jadx were downloaded to the session
scratchpad.

**Ephemeral:** the decompiled APK, Ghidra output and downloaded tools live in
the session scratchpad and will not survive. All *findings* are in
`docs/dsp408-protocol.md`; regeneration steps are in that file and in
`tools/ghidra/README.md`. Nothing needs re-deriving, only re-downloading if
further digging is wanted.

---

## Hard-won gotchas

Each of these cost real time and would not be obvious on a re-read of the code.

- **Windows applied voice noise-suppression to the capture endpoint**, an
  80 dB level-dependent expander that manufactured a convincing 70 dB/octave
  low-frequency rolloff and corrupted four measurements. Hence the mandatory
  linearity check.
- **Band-averaged raw spectra overstate low-level response** by 20 dB or more,
  because near the noise floor you are measuring noise. Use deconvolution,
  which has processing gain.
- **Round-trip latency is not repeatable** — 30 samples between consecutive
  runs, ~1600 across sessions. Never cache it.
- **MME drops samples**, producing multi-dB narrowband artifacts at
  frequencies that move between runs. Median of ≥3, and the median must be
  taken **per frequency bin**, not per time sample — a dropout is narrowband
  and is an outlier at no single time sample.
- **Passes must be aligned sub-sample before combining**, or the result
  comb-filters into something that looks like catastrophic system response.
- **Ghidra finds 82 of ~500 functions** without seeded entry points, and
  renders peripheral constants as `DAT_<literal address>` so grepping for
  `0x40010800` finds nothing.
- **A USB hub stopped the DSP enumerating entirely** — no device, no failed
  enumeration, nothing in the event log. Straight to a motherboard port fixed
  it. Second time this project has lost time to a USB path; the first was a
  charge-only cable to the phone.
- **MME device indices renumber** when the Windows default output changes, so
  the interface silently moved from output index 3 to 7. Select devices by
  host-API-qualified *name*.
- **A video playing on the host** corrupted a crossover measurement by 6% while
  leaving the curve smooth and plausible. Swept-sine rejects uncorrelated
  interference well — 6 dB in-band SNR only moved the result ~1.5 dB — which is
  exactly why it is dangerous rather than obvious.
- **A failed check can carry the answer you were about to go measure.** The
  linearity check that false-positived on OUT5 reported gain at 300, 1000 and
  3000 Hz — three points on the very crossover curve the next sweep was going
  to characterize. Fitting them settled the quantization question before the
  sweep ran: fc = 450 matched to 0.05 dB rms against 1.65 dB for the nearest
  table entry. Read the numbers in a failure before discarding it.
- **EQ bypass is destructive the moment the app session ends.** It zeroes the
  stored band levels immediately; the "restore" that brings them back lives in
  the app's session memory, not on the device. Unplug USB, close the app or
  cycle device power and the gains are gone for good — confirmed on both a
  reconnect and a power cycle. Never save a backup while bypassed either: the
  file will be missing every EQ gain, and that is **not detectable afterwards**,
  because a band at a custom frequency with 0 dB is also an ordinary thing for
  a tuner to leave behind.
- **Presets are the only device-side restore, and edits do not write through to
  them.** Preset 1 came back clean after being bypassed and power-cycled, twice.
  Store the baseline into a preset slot before any tuning run — it is a rollback
  point that survives everything our software could get wrong.
- **Reset EQ is final.** It rewrites frequency, bandwidth and level to defaults
  across all bands, and Restore EQ does not undo it. The app warns first;
  believe the warning.
- **A diff identifies an action by what it wrote, which beats remembering what
  you clicked.** The bypass-vs-reset question was corrected twice from
  recollection — once by each of us, in opposite directions — and both
  corrections were wrong. The byte signature settled it: level-only means
  bypass, because reset also moves frequency and bandwidth. When an action's
  identity matters, identify it from the diff.
- **Writes stick immediately, so there is no undo-by-reboot.** The intuition
  that a power cycle reverts uncommitted changes is exactly backwards here:
  power loss preserves, preset recall destroys.
- **In-circuit resistance is a parallel combination, not a component value.**
  Every path between the probes contributes. On a filter network that means a
  plausible-looking 4 kΩ tells you almost nothing. **Only ~0 Ω is unambiguous**,
  because only direct copper reads zero — so design board measurements around
  finding zeros and comparing two candidates, not around reading values.
- **A negative scan result is much weaker than a positive capture.** An SDP
  scan found no SPP on the DSP-408 and that was written down as "control is
  BLE-only". A capture of the vendor app later showed ~11 800 RFCOMM frames on
  exactly that SPP. A scan says what a device *offers* — or what it happened to
  answer that day; a capture says what the software *does*. Do not let the
  absence of something in a scan close a question.
- **The vendor app's delay display is exact.** Set the unit selector to ms and
  the values are integer samples at 48 kHz; reading an existing tune answered a
  question that had a whole acoustic experiment planned for it.
- **Read the saved tune before designing an experiment.** That trick has now
  paid twice: the `.DDP` backup sitting unexamined in the repo root held the
  answer to half the open M0 questions and cast doubt on a third. It is a dump
  of the wire protocol's own parameter blocks.
- **A single measurement can be ambiguous by bad luck rather than by noise.**
  The 500 Hz crossover test landed almost exactly between "honored" and
  "snapped to the nearest table entry", so a clean ±0.21 dB fit decided nothing.
  When a measurement is meant to discriminate between hypotheses, pick the
  probe point that maximizes their separation *before* running it.
- **The vendor manual's delay arithmetic is wrong** — it prints 0.294 ms/cm
  where the correct figure is 0.0294.
- **A wrong claim can still point at a real bug.** An adversarial review
  asserted that read-modify-write through blocks 31 and 35 would drop undecoded
  bytes. It does not — both decode all eight bytes and re-encode byte-exact.
  But checking it found that **block 34's decode is semantically wrong**:
  `protocol.py` calls it `MIX_IN_9_16`, the device returns dynamics-shaped bytes
  there, and `ddp.py` has always called it "dynamics A". Two modules disagreeing
  since the day both were written, found by investigating a claim that was
  itself false. Chase the claim, not the verdict.
- **A defence that exists only in prose is not a defence.** The same review found
  "blast-radius caps" and "two-key arming" written up as load-bearing protections
  with no module, no function and no test behind them. They read exactly like
  implemented features in a design document.
- **A green suite against a simulator confirms your assumptions, not the
  hardware.** Most of these tests run against `sim.py`, which encodes our model
  of the device — including the `DeviceLimits` pool model we know is wrong. The
  only genuinely independent checks are the ones against captured traffic and
  the vendor app's own files.
- **Order is part of correctness on a device with no undo.** A counter-review of
  this session's plan found no design errors and three sequencing errors, one of
  which — recalling a preset before archiving the working area — would have
  destroyed the in-car tune while every individual step looked reasonable.
- **A degenerate fit can look excellent.** A peaking section with an absurdly low
  Q flattens into a broad tilt that matches almost anything smooth — a synthetic
  shelf fits to 0.13 dB rms at Q ≈ 0.03. **A low residual is not on its own
  evidence that the model is right.** Check the fitted parameters are ones the
  device could actually hold.
- **Pick the probe point that separates the hypotheses, before running.** A
  peaking filter's half-gain points and its −3 dB points coincide at exactly
  +6 dB, so a bandwidth-convention test at 6 dB answers nothing. This is the
  second time the project nearly ran a null experiment; the first was the 500 Hz
  crossover that landed halfway between "honoured" and "snapped".
- **Device-side gain is downstream of the safety limiter.** `tuner.safety` caps
  what we transmit; the driver gets that plus the DSP's channel gain and any EQ
  boost. Nothing in the safety layer can see it. A +12 dB band turns a −18 dBFS
  stimulus into 0 dBFS at the speaker.
- **Two sweeps beat one when measuring a filter.** A reference sweep with the
  band flat, divided out, cancels the speaker, room, microphone, interface and
  cable exactly — they are identical in both. Recovers Q to 1 part in 10⁴ where
  a single sweep, forced to absorb all of that into one offset term, is >5% out.
- **Coordinate-wise medianing of complex spectra is not a median.** Three unit
  phasors 120° apart give magnitude 0.5. It cost the measurement engine up to
  7.6 dB at high frequency when alignment degraded — and it survived for months
  because `_combine_passes` had no direct tests at all.
- **Folding a control channel into a data stream is invisible until it isn't.**
  `rfcomm_streams` did not filter by DLCI, so RFCOMM's multiplexer control
  channel contributed 18 bytes to the head of each direction. Frame *recovery*
  was unaffected, because frames are found by preamble — but every byte offset
  was 18 out, which would have broken any comparison against the real stream.
