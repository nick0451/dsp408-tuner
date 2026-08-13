# Bench session — run sheet, and now the record

> ## ✅ Done 2026-08-09. This is a record, not a plan.
>
> Everything below ran. The session closed every open protocol question except
> blocks 34/35 and the channel-to-chip mapping, and it went well past the two
> jobs it was written for.
>
> | | Result |
> |---|---|
> | **D0** combiner cross-check | 450.1 Hz / 0.247 dB vs 449.4 / 0.241 — the new `_combine_passes` did not move the instrument |
> | **D1** RBJ filter shape | Half-gain convention, ±0.8 % across a 4.6× bandwidth span, symmetric in boost and cut |
> | **D1** LF resolution | `fc` continuous at 25 Hz — 25/27 Hz fit to 24.9/26.9, exactly 2.0 Hz apart |
> | **D2** preset opcodes | `user_id` selects the slot; **a recall is eight READs**, no select opcode. Six slots, not fifteen |
> | **D2** mute, master volume, mix | All decoded on the wire |
> | **D2** `DataType 3` | Unreachable from the Android app — that is why it never appeared |
> | **Link mirroring** | The app mirrors, the device does not — 6 writes linked, 3 unlinked |
> | **Clean disconnect** | There is none. Polling stops mid-stream |
> | **Crossover slope / alignment** | Mapped by 14 A/Bs; slope now writable |
> | **`EqBand.type`** | 0 PEQ / 1 low shelf / 2 high shelf — and it *added* a refusal |
> | **REW golden** | 0.35 dB max / 0.09 rms over 30 Hz–3.5 kHz. The debt is paid |
> | **HF artifact** | Was MME below 10 kHz and REW's own scatter above it |
>
> Detail in [dsp408-protocol.md](dsp408-protocol.md) and [STATE.md](STATE.md).
> **The next session is first contact with the device** — the staged bring-up in
> STATE.md, on the bench, with nothing wired to the outputs.
>
> Kept intact below because the *method* is reusable: how the ordering was made
> non-destructive, why +6 dB is a null test, why the archive has to precede the
> first mutating measurement, and what each check was for.

**Written 2026-08-09, revised the same day.** One visit, in this order:

| | Job | Why here |
|---|---|---|
| **Before anything** | Save a `.DDP`, **store the working tune to a preset slot** | D1 mutates the tune. The archive has to precede it. |
| ~~**D0**~~ | ~~Reproduce the 449.4 Hz corner on OUT5~~ | ✅ **Done 2026-08-09: 450.1 Hz, residual 0.247 dB.** The combiner did not move the instrument. |
| ~~**D1**~~ | ~~PEQ shape vs RBJ, on OUT1~~ | ✅ **Done 2026-08-09: RBJ half-gain confirmed to ±0.8 % across a 4.6× bandwidth span, symmetric in boost and cut.** Results in `docs/dsp408-protocol.md`. |
| **Handover** | Recall the slot, diff against the pre-session file | Restores the tune *and* rehearses D2 step 4 for free. |
| **D2** | Second HCI capture | Churns preset state, so it goes last. |

The ordering is load-bearing at three points and each one is flagged where it
matters. Do not compress it.

### Decided 2026-08-09: this is a bench session, on an electrical loopback

The differential method means D1 *could* be done in-car with a microphone — the
reference sweep cancels the car. **The operator chose the bench anyway, and the
reasoning is worth recording because it will come up again:**

> The in-car microphone path on this Windows host is flaky, which caps how many
> runs are practical before something goes wrong. Wiring the Scarlett to the DSP
> gives several independent sources of truth at once and removes the operator
> steps most likely to invalidate a run — mic placement, ambient noise, a level
> nudged between sweeps.

Fewer failure modes beats fewer trips. An electrical loopback also removes the
speaker and the room entirely rather than cancelling them, so the differential's
out-of-band residual becomes a genuine check on the *instrument* rather than on
whether anyone moved.

Keep the in-car differential in mind for anything that must be measured with the
unit installed — it is validated and it works. It just is not the cheapest route
to this particular answer.

> ### ⚠ Read this before touching a preset
>
> **Preset recall overwrites the working area, edits do not write through to
> presets, and there is no undo.** So the live tune must be archived *before*
> anything recalls a preset — not after. An earlier draft of this run sheet had
> the recall first and would have destroyed the working tune at step 2.
>
> The ordering in D2 exists for that reason. Do not reorder it.

---

## Before anything

> ### ⚠ Corrected 2026-08-09: archive *before* D1, not before D2
>
> The original sheet put the archive steps inside D2, on the assumption that D1
> was a read-only measurement. **It is not.** D1 flattens a channel's crossovers
> and all 31 of its EQ bands, and every one of those writes is immediately
> non-volatile. So the archive step that protects D2 has to protect D1 as well,
> and it has to happen first.

1. **Save a `.DDP` of the current state.** Standing rule. Name it
   `pre_session_2026MMDD.DDP` and do not overwrite any existing backup.
2. **Store the working state to the sacrificial preset slot** (slot 15 — see
   "The sacrificial preset slot" under D2, and confirm it is expendable first).
   Give it a distinctive name such as `bench0809`.

   **Then confirm the name appears in the app's preset list.** That is the only
   verification available: storing a preset does not alter the working area, so
   a `.DDP` saved afterwards cannot tell you whether it landed. If the name is
   not there, stop — the recall in step 4 of the D1/D2 handover would then load
   whatever `lbass` was in the slot and take the working tune with it.

   **Corrected 2026-08-09:** this previously said a `.DDP` is a record with no
   demonstrated way back. **It is a working restore path** — see "What D0 also
   settled". Store the preset anyway: it needs no host, no app and no file, and
   two independent restore paths cost one action.
3. **Confirm the analog knobs**: monitor hard at maximum, input gain hard at
   minimum. Both are on end stops, which are the only reproducible positions an
   analog knob has.
4. Configure over USB-B, then **unplug USB before measuring** — the USB ground
   path adds a 100 Hz harmonic series 43 dB above the clean floor. Straight to a
   motherboard port; a hub stopped the unit enumerating entirely.
5. Select audio devices **by host-API-qualified name**, never index.
6. Run the linearity check once, with tones inside the channel's passband.

### Wiring

`bench_peq.py` hardcodes output channel 0 and input channel 1, so:

* **Scarlett left monitor output → DSP input.**
* **DSP output 1 → the Scarlett's 1/4" input 2**, with **INST switched off**.
  Instrument mode is high-impedance and far more sensitive; a line-level DSP
  output into it will clip.

Set the input trim before the *reference* sweep and then touch nothing analog —
not the trim, not the monitor knob — until the run finishes. A level that moves
between the two sweeps of a differential pair breaks the cancellation, and the
tool's out-of-band residual is what will catch you.

---

## D0 — ✅ **Done 2026-08-09. The combiner did not move the instrument.**

| | 2026-08-08, old combiner | 2026-08-09, new combiner | Δ |
|---|---|---|---|
| Corner | 449.4 Hz | **450.1 Hz** (+0.03 % vs set) | 0.7 Hz, 0.16 % |
| rms residual | 0.241 dB | **0.247 dB** | 0.006 dB |
| Snap-to-420 hypothesis | +6.99 % out | **+7.17 % out** | still refuted |

Linearity 0.03 dB spread across level. **Every figure measured under the old
`_combine_passes` stands.**

Better than a reproduction: the operator had loaded
`dspcartunebackups_flat_channel_1_diff.DDP`, whose OUT5 record is **byte-identical
to `dspcartunebackups.DDP`'s** and differs from the `ch5_neg_19db` files only in
`gain_raw` — the exact configuration the 449.4 Hz figure came from.

Run at −20 dBFS rather than the default −6, so `--electrical-only` was not
asserted. Passband offset therefore reads −21.93 dB against −5.91 dB before;
14 dB of that is stimulus level (`magnitude_dbfs` is **not** level-normalised —
deconvolution divides by the *unscaled* sweep), leaving ~2 dB, which says no
analog knob has moved since 08-08. A flat offset cannot move a corner
frequency, and the residual agreeing to 0.006 dB confirms it did not.

<details>
<summary>Original D0 instructions, kept for the method</summary>

### Reproduce the 449.4 Hz corner first. One cable move, and it is owed.

**`_combine_passes` changed on 2026-08-09.** Passes are now combined as
median-magnitude × coherent-sum-phase rather than a coordinate-wise complex
median, which is a change to the code path underneath *every* measurement this
project has ever made. Nothing on the bench has been measured through the new
version.

There is exactly one prior result precise enough to serve as a control, and
reproducing it costs one cable move:

| Prior run | Conditions |
|---|---|
| Corner **449.4 Hz** (−0.14 %), rms residual **0.241 dB** | OUT5's 450 Hz **low**-pass, swept fit 200–1600 Hz, 400 points, 2026-08-08 |

So do this **before** anything else, with the Scarlett input on **OUT5** and
OUT5 left exactly as the tune has it:

```bash
python tools/bench_crossover.py --lp 450 --fit 200 1600 --points 400 --electrical-only
```

Then move the cable to OUT1 and carry on. Agreement inside the 0.39 dB
repeatability floor — and a corner within a few tenths of a hertz — says the new
combiner did not move the instrument, and every figure measured under the old
one still stands. Disagreement is the single most important finding of the
session and stops it: nothing measured afterwards would be comparable to
anything measured before.

This is deliberately a *reproduction*, not an equivalent measurement. OUT1's
450 Hz high-pass is right there and would need no rewiring, but it is a
different filter on a different channel and could not distinguish "the combiner
changed the answer" from "the two channels differ".

</details>

### What D0 also settled, neither of which it was aiming at

**1. USB-B was still connected on the first attempt, and the linearity gate
caught it.** Same rig, same channel, one cable:

| | USB-B connected | USB-B removed |
|---|---|---|
| Gain spread across level | **5.55 dB — `NonLinearPath`** | 0.03 dB — linear |

The failing table was *scattered* rather than monotonic — the −30 dBFS row held
both the highest and the lowest reading — which is the signature of tones in the
noise, not of a compressor. That distinction matters because the two failures
lead to opposite actions: "the chain is invalid, stop" versus "turn it up".

This is the 43 dB USB ground-path finding reproduced live, and it is the second
time `require_linear_path` has stopped a corrupted chain before it could produce
a smooth, plausible, entirely wrong curve.

**2. The vendor app can load a `.DDP` back to the device — proven by
measurement.** The operator loaded the file rather than re-entering it by hand,
and the sweep confirms it landed on the hardware rather than merely in the app:

* the *previous* working tune had OUT5 band 1 at **286 Hz, −5.8 dB**, inside the
  200–1600 Hz fit window. A dip that size cannot coexist with a 0.247 dB LR4
  residual. It is not there.
* that tune also had `l_level` **1** where the loaded file has **3**. An LR4 fit
  that clean is inconsistent with a different rolloff order.
* the device demonstrably held the old state 38 minutes earlier
  (`eq_channel1_no_mute.DDP`, 17:09). The load is the only intervening event.

**This changes the rollback story.** `.DDP` files were treated as a record with
no demonstrated way back; they are a working restore path. It does not replace
the preset slot — that one needs no host, no app and no file — but it means a
file backup is recoverable, which the improvement invariant was not previously
entitled to assume.

---

## D1 — Does a device EQ band actually produce an RBJ curve?

**Why this first.** `tuner.optimize.biquad` fits RBJ biquads and converts
`bw_raw` → octaves → Q by the standard peaking relation. **Nobody has ever
measured that the device agrees.** Bandwidth definitions differ between
implementations — −3 dB points, half-gain points, others — and if the firmware
uses a different one, every fit is systematically wrong in a way that looks
like a mediocre optimizer rather than a wrong model.

This is the same class of error as the frequency-table assumption already
caught, and it needs **no new code**: the vendor app sets the band, the existing
validated rig sweeps it, `biquad.py` predicts it. Exactly the method that closed
gain and frequency end to end.

### The trap this test would have fallen into

**Do not run this at +6 dB.** A peaking filter's half-gain points sit at `G/2`
and its −3 dB points at `G−3`. Those are *equal* when `G = 6`, so at +6 dB the
two candidate bandwidth conventions predict **the same curve** and the
experiment answers nothing. An earlier draft of this run sheet said +6 dB.

At **+12 dB** they are 3 dB apart, and the gap widens with bandwidth — verified
in `tests/test_bench_peq.py`:

| `bw_raw` | octaves | Q | half-gain vs −3 dB width |
|---|---|---|---|
| 25 | 0.30 | 4.800 | 0.14 oct apart |
| 65 | 0.70 | 2.041 | ~0.4 oct apart |
| 134 | 1.39 | 0.999 | 0.51 oct apart |

That widening is why one bandwidth is not enough: a narrow band's two
conventions could hide inside fit noise, a wide one's cannot. All three
`bw_raw` values above are confirmed representable — they sit inside the 24–134
range observed across every saved tune in the repository.

### Configure the channel under test — use OUT1

**OUT1**, because it is already the damaged channel (LPF 1234 Hz, band 2 at
12699 Hz / +8.9 dB), so flattening it destroys nothing that is not already
destroyed; because it is not link-grouped the way outputs 7 and 8 are; and
because `dspcartunebackups_flat_channel_1_diff.DDP` already holds exactly this
configuration, so it can be copied rather than invented.

| Control | Set to | Why |
|---|---|---|
| Output enabled | on | settled 2026-08-09; byte 0 of MISC, 1 = on |
| High-pass | minimum (20 Hz) | required for the 25/27 Hz runs |
| Low-pass | maximum (20 kHz) | see below |
| All 31 PEQ bands | level 0 dB (`level_raw` 600) | a flat reference sweep makes any anomaly visible directly |
| PEQ bypass | **off** | a `.DDP` saved while bypassed has its band gains zeroed and is silently incomplete |
| Dynamics / limiter | off | a compressor invalidates every single-level measurement |
| Gain, delay, polarity, mix | **leave alone** | magnitude-only test; all cancel differentially, and each control left alone is one fewer to restore |

**Use one band for every run** — band 2 is the natural choice, being displaced
already. One non-flat band means the `.DDP` diff between runs is a single block
and the stored `bw_raw` reads back unambiguously.

> **Why the crossovers must be parked even though the differential cancels
> them.** They *do* cancel — they are present in both sweeps. What does not
> cancel is signal-to-noise. The default fit window is four octaves either side,
> so a 1000 Hz band is fitted from 62 Hz to 16 kHz; with the low-pass at 1234 Hz
> the top half of that window is 30 dB down and the divided result is noise over
> noise. The fit would report `POOR FIT` for a filter that is perfectly correct.
> The cancellation is exact in theory and irrelevant in practice.

### Procedure — differential, two sweeps per setting

**This does not require removing the DSP from the car.** Take a reference sweep
with the band flat, then the same sweep with the band engaged, and divide. The
speaker, the cabin, the microphone, the interface and the cable are identical in
both, so they cancel exactly. What remains is the filter and nothing else.

Verified on synthetic data through a deliberately hostile system — a 12 dB/decade
tilt, an 8 dB room mode and two 4th-order rolloffs — and it recovers Q to 1 part
in 10⁴ (`tests/test_bench_peq.py::TestDifferentialMethod`). The single-sweep fit
on the same system gets Q wrong by more than 5%.

> ### ⚠ The boost is downstream of the safety limiter
>
> `tuner.safety` caps what **we transmit**. The band's +12 dB is applied inside
> the DSP, after that. The driver gets stimulus + 12 dB, and nothing in the
> safety layer can know.
>
> **So drop `--level-dbfs` by at least 12 dB from whatever you would normally
> use.** The tool prints this warning too. On the bench with nothing connected
> to the output, pass `--electrical-only` and it does not arise.

The tool prompts for each step, so you can stand at the app. One command
per line, no continuations -- these run in PowerShell, where a trailing
backslash is not a line continuation and would truncate the command:

```bash
# Electrical loopback: nothing but the Scarlett line input on the DSP output.
# -18 dBFS + the band's 12 dB lands at -6 dBFS, clear of both clipping and the
# noise floor.
python tools/bench_peq.py --differential --electrical-only --level-dbfs -18 --freq 1000 --bw-raw 25 --gain-db 12
python tools/bench_peq.py --differential --electrical-only --level-dbfs -18 --freq 1000 --bw-raw 65 --gain-db 12
python tools/bench_peq.py --differential --electrical-only --level-dbfs -18 --freq 1000 --bw-raw 134 --gain-db 12

# A cut needs no headroom, so it can run hotter and sit further above the floor.
python tools/bench_peq.py --differential --electrical-only --level-dbfs -6 --freq 1000 --bw-raw 65 --gain-db -12
```

`--electrical-only` is the deliberate act rule 4 requires: it asserts nothing but
a line input is on that output, which is what permits a level above the
tweeter-safe default. **Confirm that is actually true before typing it.**

Each run asks you to set the band flat, sweeps, then asks you to set it to the
stated value and sweeps again. **Save a `.DDP` at the engaged setting** and read
the actual stored `bw_raw` back with `ddp_dump.py --eq` — the app's Q display is
pre-rounded, and feeding a rounded Q back through `bw_raw_for_q` is exactly the
mistake that produced two wrong predictions earlier in this project. If the app
will not accept a Q landing on the `bw_raw` you want, take what it gives and pass
that.

The last row is a cut rather than a boost, because some implementations are
asymmetric. It needs less headroom, hence the different level.

### One extra run: the internal-headroom null test

The +12 dB is applied **inside the DSP**. Its own signal path therefore needs
12 dB of headroom too, and nothing on this bench can see whether it has it. If
the DSP clips internally, the fitted gain comes back below +12 dB with a raised
residual — which reads as *the model is wrong* when the truth is *the level was
wrong*. Those two look identical in the output and lead to opposite actions.

Separating them costs one run:

```bash
python tools/bench_peq.py --differential --electrical-only --level-dbfs -24 --freq 1000 --bw-raw 65 --gain-db 12
```

Same band, 6 dB quieter. If the fitted centre, Q and gain match the `-18` run,
nothing is clipping and the `-18` results stand. If they differ, the `-18` runs
are level-limited and all four need repeating lower. Run this **second**, right
after the first `-18` run at `bw_raw 65`, so a headroom problem is found before
three more runs are spent on it.

Note that the tool's own linearity check does **not** cover this: it runs before
the differential prompts, so it measures the channel in whatever state it was
left in — with the band flat, not boosted.

**Check the out-of-band residual the tool prints.** Far from the band the two
sweeps should differ by ~0 dB. If they do not, something moved between them — the
mic, a level, or the wrong band got edited — and the fit is not trustworthy.

### Low-frequency resolution, while set up

Two mid-band points (450 Hz, 1234 Hz) do not tell us whether `fc` stays
continuous down where ADAU1701 5.23 fixed-point coefficients bite.

```bash
python tools/bench_peq.py --differential --electrical-only --level-dbfs -18 --freq 25 --bw-raw 65 --gain-db 12 --fit 15 200 --repeats 5
python tools/bench_peq.py --differential --electrical-only --level-dbfs -18 --freq 27 --bw-raw 65 --gain-db 12 --fit 15 200 --repeats 5
```

**The fit low edge is 15 Hz, not 10, and the repeats are raised.** The sweep runs
10 Hz–20 kHz in 2 s, so the bottom octave gets very little energy, and the
channel's 20 Hz high-pass takes more out of it. The differential cancels that
response exactly but not its effect on noise, so fitting down to 10 Hz means
fitting noise. If these two runs still look ragged, raise the low edge again
rather than believing the fitted centre.

If both report the same fitted centre, `fc` is quantized down there and the
optimizer must not search LF continuously. Two points 2 Hz apart is a deliberate
choice: coarse enough that any plausible quantization step separates them,
fine enough that a continuous parameter resolves them easily.

### Reading the output

The tool answers two questions separately, because they fail differently.

**1. Is the shape an RBJ peaking section?** Reported as the rms residual with
every parameter free. Under ~0.4 dB (the repeatability floor) means yes.

> A low residual on its own is **not** proof. A peaking section with an absurd Q
> flattens into a broad tilt that fits almost anything smooth — a synthetic
> shelf fits to 0.13 dB rms at Q ≈ 0.03. The tool therefore also checks the
> fitted Q against the range the device can actually store and says
> `LOW RESIDUAL BUT DEGENERATE` if it escapes. That check exists because a test
> caught the fitter doing exactly this.

**2. Is our Q mapping right?** Reported as the fitted filter's measured width at
each candidate convention, against the octaves the device was told to use. One
of three verdicts:

- `matches: half-gain points` — our model is right, nothing changes.
- `matches: -3 dB from the peak` — **`q_from_bw_raw` is wrong and every
  optimizer fit is skewed.** Fix it before trusting any tune.
- `NEITHER convention matches` — check the saved `.DDP` before concluding
  anything about the firmware; the likeliest cause is that the band was not set
  to the values passed on the command line.

**The verdict comes from how the error moves across the three bandwidths, not
from any single run.** Record `bw_raw`, the fitted Q and the residual for each.

---

## Between D1 and D2 — restore, and get a free known-answer test

D1 leaves OUT1 flattened. D2's step 4 checks preset recall against the OUT1
fingerprint (12699 Hz, 1234 Hz, +8.9 dB), so that fingerprint has to be back in
the working area before D2 starts. Restoring it here is not overhead — it is the
same round-trip D2 step 4 tests, run once without the capture attached.

1. **Save `post_d1_2026MMDD.DDP`.** This is the record of exactly what the test
   channel was set to, and the authority on the `bw_raw` values the runs
   actually used.
2. **Recall the sacrificial slot** stored in "Before anything" step 2.
3. **Save `post_restore_2026MMDD.DDP` and diff it against the pre-session file:**

   ```bash
   python tools/ddp_dump.py pre_session_2026MMDD.DDP post_restore_2026MMDD.DDP
   ```

   **Expected: no differences.** Anything else is a finding about how store and
   recall handle the working area, and it is worth more than the rest of the
   session — write down exactly what differs before doing anything about it.

D2's step 2 then stores to the same slot again, with the snoop log running, to
capture the opcode. Doing it twice is deliberate: the store here is the
protection, the store there is the measurement.

---

## D2 — Second HCI capture

Closes five "unknown, design around it" items. Enable the snoop log and restart
Bluetooth first, exactly as last time.

### ~~D2a — the mute A/B~~ — **done 2026-08-09, before the session**

Settled ahead of the bench trip. Muting output 1 in the vendor app changed
**exactly one byte in the whole backup**: MISC byte 0, `1 -> 0`, with `gain_raw`
untouched at 500.

So the field is `enabled` (1 = on), the sense is inverted from the APK's `mute`
name, and muting is a separate control rather than a gain zeroing. `protocol.py`
renamed, `Dsp408Spp` now writes it, evidence pinned in
`tests/test_bulk_record.py`. **Nothing to do at the bench.**

### The sacrificial preset slot

Already chosen and written to under "Before anything" — the ordering correction
moved it there, because D1 needs the same protection D2 does.

Slots 7–15 all read `lbass` and look like duplicates, but **that is an inference
from names, not confirmation.** Confirm one is expendable before overwriting it.
**Slot 15** is the natural candidate. Write down which slot you used; slots are
finite and one now holds test data.

> ### ⚠ Revised 2026-08-09, after D1: do not restore before D2
>
> The handover section above has the operator load `eq_channel1_no_mute.DDP`
> before D2, so step 4's known-answer check can look for the OUT1 fingerprint in
> the table below. **Skip it.** Two reasons, and the second is the important one:
>
> 1. **The fingerprint is stale, and what replaced it is better.** D1 left the
>    working area holding `HPF 20 / LPF 20000 / band 1 at 27 Hz, +12.0 dB,
>    bw_raw 65` — a configuration no preset on the device could plausibly
>    contain. As a recall round-trip check that beats the damaged-tune
>    fingerprint, and it is already there.
> 2. **Restoring first ends the session on the wrong tune.**
>    `eq_channel1_no_mute.DDP` *is* the damaged tune — it carries the snoop
>    accidents that are on the repair list. Preset 4 `lbass` is the intended base
>    tune and step 8 recalls it regardless. Restoring first is a USB round-trip
>    to install something the sequence then deliberately overwrites.
>
> Nothing is lost: the state is in the repo, and it was pushed to the phone at
> `/sdcard/Download/` so it can be loaded back at step 8a if wanted.

### Useful fact: the current OUT1 state is known and damaged

From the last snoop session, output 1 carries:

| Parameter | Was | Now |
|---|---|---|
| Low-pass | 3500 Hz | **1234 Hz** |
| Band 2 frequency | 486 Hz | **12699 Hz** |
| Band 2 level | 600 (0 dB) | **689 (+8.9 dB)** |
| Band 2 bandwidth | 25 | **106** |
| Band 3 bandwidth | 42 | **110** |
| Band 3 level | 480 (−12 dB) | **600 (0 dB)** |
| Gain | 500 | 500 — back to original |

That is a **distinctive fingerprint**, which makes step 4 a known-answer check
rather than a hopeful one. The base tune is preset 4, `lbass`, so step 8 also
performs the repair already on the to-do list.

### The sequence — do not reorder

| # | Action | Why |
|---|---|---|
| 1 | Connect. **Sit 2 minutes with no interaction.** | Records whether the app ever stops polling. See the note below. |
| 2 | **Store the current state to the sacrificial slot.** | Captures the **store opcode**, and creates a device-side rollback point *before* anything is at risk. |
| 3 | **Recall a different preset** — slot 1, `re-timed`. | Captures the **recall opcode**. |
| 4 | **Recall the sacrificial slot from step 2.** | Recall a second time, and proves the round-trip: the OUT1 fingerprint above must come back exactly. |
| 5 | **Mute one output, then unmute it.** | The *meaning* of byte 0 is already settled (D2a). What is still unseen is the **wire form**: confirm the app sends an ordinary `DataType 4` MISC-block write and not a separate opcode. Cheap, and it closes the field end to end. |
| 6 | **Toggle the link on outputs 7/8, then toggle back.** | Settles device-mirrors versus app-sends-two-writes. |
| 7 | **Change one input setting, then revert it.** | `DataType 3` is entirely unobserved. **Must be reverted.** |
| 8 | **Recall preset 4 (`lbass`).** | Leaves the car on its intended base tune. |
| 8b | **Load a `.DDP` to the device**, *if the Android app can* — the pre-session file is the natural choice, since step 8 has just left the tune where that file wants it. | **New 2026-08-09.** A `.DDP` load is now known to reach the device, but *how* is unknown: 553 individual `DataType 4` writes, or a bulk opcode never seen? If the latter, it is a write path whose blast radius is the entire device and `txpolicy` must know about it before anything of ours touches the transport. **Caveat:** the observed load was the Windows app over USB-B, so this step depends on the Android app offering the same function. If it does not, the finding needs a USB capture instead and this step is simply skipped — do not force it. |
| 9 | Disconnect from within the app. | A real shutdown sequence — the last capture ended with a link timeout instead. |

### Afterwards — extraction commands

Save a `.DDP` first, then pull the log. Re-pair wireless ADB if needed
(Developer options → Wireless debugging), or use `adb bugreport` over USB.

```bash
# 1. Pull the capture. The bug-report zip is accepted directly -- the log path
#    varies by vendor and is not readable without root on Android 9+.
adb bugreport bugreport-2026MMDD.zip

# 2. What is in it, and did the preset actions land?
python tools/btsnoop_extract.py bugreport-2026MMDD.zip

# 3. Just the writes -- the app polls ~10/s, so an unfiltered dump is
#    thousands of reads around a handful of writes.
python tools/btsnoop_extract.py bugreport-2026MMDD.zip --writes --payloads

# 4. Confirm the recall round-trip by looking for the OUT1 fingerprint.
#    12699 Hz and 1234 Hz should reappear after step 4's recall.
python tools/btsnoop_extract.py bugreport-2026MMDD.zip --expect 12699 1234 689

# 5. Diff the tune against where it started (two positional files = diff).
python tools/ddp_dump.py pre_session_2026MMDD.DDP post_session_2026MMDD.DDP
```

**The single most important thing to check** is whether steps 2 and 3 produced
any write that is *not* `OUTPUT_CHANNEL`. Every write in the first capture is
`DataType 4`; there is no host write with `DataType 9` anywhere in it. So the
preset opcodes are expected to be something we have never seen:

```bash
python tools/btsnoop_extract.py bugreport-2026MMDD.zip --writes --payloads \
  | grep -v OUTPUT_CHANNEL
```

Anything that survives that filter is new. If it is empty, the preset actions
either did not reach the wire or use a frame shape the reader does not
recognise — in which case send me the zip rather than concluding either way.

Whatever does appear is the thing the whole rollback story has been waiting for.

> **What step 1 actually tests.** The first capture shows 2 866 identical polls
> over 286 s, so the app almost certainly polls regardless of interaction —
> meaning "2 minutes idle" still carries the keepalive and does **not** answer
> whether the link survives without it. Step 1's value is recording whether the
> app ever stops. The keepalive question gets answered by *our* backend at
> bring-up Stage 2: connect, send nothing, time the drop.

### Timestamps — take them from the phone's clock, not by hand

**Improved 2026-08-09.** btsnoop timestamps are in *device* time, so stamping the
phone's own clock at each step aligns to the frame exactly:

```bash
adb shell date +%s.%N
```

`adb` is at `C:\Users\nick\platform-tools\adb.exe`. It was previously only inside
a *session-scoped temp scratchpad* from an earlier run, which is why it went
missing and took a full-drive search to find; it has been copied somewhere
durable.

The last capture's writes had to be matched to actions from recollection, which
is how it emerged that the recollection was wrong about which channel had been
edited. The wire wins either way — precise stamps just make it findable in
seconds instead of by inference.

### Vendor app package

`leon.android.chs_ydw_dcs480_dsp_408` — useful for `adb shell am start`,
`pm clear`, or confirming which app was foreground when a frame was sent.

---

## What this unblocks

| Finding | Unblocks |
|---|---|
| Filter shape vs RBJ | `optimize/biquad.py`'s core premise; everything M4 fits |
| How a `.DDP` load reaches the device | Whether a whole-device bulk write opcode exists, and therefore what `txpolicy` must refuse |
| Preset store + recall opcodes | The only device-side rollback; the improvement invariant's rollback half |
| Mute vs enable | Exposing mute through the backend at all |
| ~~Link write semantics~~ | ✅ **Settled 2026-08-09: the app mirrors, the device does not.** Outputs 7/8 writable via `Dsp408Device.modify_block_mirrored()` |
| `DataType 3` | Input-side control, entirely unmodelled — and **unreachable from the Android app**, so a Bluetooth capture will never show it |

## What is deliberately **not** in this session

- **No writes from our code.** M3 has no transport yet. Everything here is the
  vendor app plus the measurement rig.
- **No preset slot beyond the one sacrificial slot.**
- **No channel-to-chip probing.** Parked until the logic analyzer arrives.
