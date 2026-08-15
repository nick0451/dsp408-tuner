# Car session runbook

For the laptop, in the car. Written 2026-08-14, before the first attempt.

> ## ⛔ DO THIS BEFORE THE DSP GOES BACK IN THE CAR
>
> **OUT1 and OUT2 are currently set to 20 Hz – 20 kHz.** They were opened for
> bench work, where they fed a self-protected plate amp. In the car those two
> outputs are the **FaitalPRO 4FE35** — 4-inch, **1.73 mm Xmax**, 30 W RMS.
> Full-range into those is destructive.
>
> They must be back at **450 – 3500 Hz** before any signal reaches the car.
>
> ```bash
> # the last snapshot with the car's own crossovers on OUT1/2
> python tools/bench_flatten.py restore \
>     --address 00:13:EF:A0:09:10 \
>     --snapshot snapshots/2026-08-12_undo.json
> ```
>
> Then prove it, and do not skip this:
>
> ```bash
> python tools/car_preflight.py --address 00:13:EF:A0:09:10 \
>     --outputs 1 2 3 4 5 6 7 8 --input-gain <what the iOS app shows>
> ```
>
> Every snapshot through `2026-08-12_*` has OUT1/2 at 450–3500. Everything
> from `pre-bench-2026-08-13` onward has them opened. Verified by reading the
> crossover block out of all 24 snapshot files.

## The signal chain

| | |
|---|---|
| Fit / analysis | **REW Pro**, as used for the Harman and smile tunes |
| Stimulus out | **Scarlett Solo, L and R** |
| Into | DSP inputs, per the operator's channel assignment |
| Mic | UMIK-1, USB, its own clock — split-clock capture, magnitude only |

**Not the laptop's onboard sound card.** Measured on the bench 2026-08-14: the
Realtek output added 6.5–12 dB of broadband noise over the alternative. The
operator's judgement that "the laptop soundcard is worse than bluetooth" is
consistent with that, and the Scarlett is better than both.

**Not Bluetooth A2DP, ever, for a sweep.** Measured the same day: 33.7 ms of
latency jitter between runs and an **11.6 dB rms** repeatability floor.
Swept-sine deconvolution assumes a time-invariant channel and a Bluetooth link
is not one. A steady tone through the same path measured correctly, which is
what makes this trap survivable only if you look for it.

## Order of work

1. **Restore OUT1/2.** Above. Nothing else happens first.
2. **`car_preflight.py`** on every output. It refuses on a wrong corner, a
   slope under the driver's floor, a gain over the clip point, a master
   volume over 54, and an undeclared input gain.
3. **Declare the setup token.** Where the mic is, which seat, doors, windows,
   HVAC, who is in the car. It is compared literally between measurements and
   it is what makes a before/after comparison mean anything.
4. **Level linearity.** `require_linear_path`. It is the one rig precondition
   that is not automatic, and skipping it on the bench cost most of an evening
   — a plate amp limiter was diagnosed three other ways before the check that
   exists for it was run. A car has a head unit and an amplifier, both of
   which can compress.
5. **Repeatability floor**, spread across the run rather than back to back.
   `--spacing-s`. A floor measured in 30 seconds understates what a run
   lasting minutes is judged against.
6. **Baseline sweeps.** Expected and warranted. Flat EQ, per channel.
7. Fit, write, re-measure, verify, and roll back on anything but an accepted
   verdict.

## Limits, enforced in code

`tuner.orchestrate.carlimits`, tested in `tests/test_carlimits.py`.

| | limit | enforced |
|---|---|---|
| Master volume | **54** | yes — read from `dt9/ch5` byte 0 |
| Input gain | **90** | **declared, then checked** — unreadable over our protocol |
| Output channel gain | **54 on the app = −6.0 dB** | yes |
| Crossover corners | per driver | yes |
| Crossover slope | per driver floor | yes, plus a warning below the recommendation |
| EQ flat for a baseline | — | yes, with `--require-flat` |

**Input gain cannot be read.** `DataType 3` has never appeared in any vendor
app capture on any platform; the iOS app is the only known path to it. So the
check is a declaration and a comparison, not a measurement, and omitting the
declaration is a refusal rather than a default. That asymmetry is the whole
value of it.

## The cabin is a hostile environment, and the compromises are known in advance

Worth deciding these before they are decided by fatigue at the kerbside.

- **Independent validation stops at 3.5 kHz.** Above it our engine is *more*
  repeatable than REW was, but nothing external has checked it. The tweeters
  live up there. Confidence in a tune is band-dependent; say so in the report.
- **Absolute delay is unavailable without a hardware loopback.** With the
  Scarlett's second output free this is fixable — one cable from output R to
  an input gives `TimingReference.LOOPBACK` — but that competes with using R
  as a stimulus channel. Decide which before wiring.
- **Narrowband error is nearly invisible to the objective.** A 6.3 dB error in
  one band moved the score by 0.23 dB. The improvement invariant catches a
  tune that is broadly worse and will accept one that is badly wrong in a
  narrow band. Look at the curve, not only the verdict.
- **Fitting below the noise floor.** The room will be loud. Tones without
  margin over the measured idle floor are excluded by `usable_against(idle)`;
  a channel with too few usable tones goes *indeterminate* rather than
  passing, and that is the correct outcome.

## Open, and it gates a level rise on CH5/6

**Which CT Sounds Meso 6.5 is installed, and what is its Fs?** The subwoofer
variant of that model publishes **Fs 56.3 Hz**, and those channels are
high-passed at **65 Hz** — essentially on resonance, where excursion peaks and
a 12 dB/oct filter protects least. Until it is answered, treat 65 Hz as the
lowest corner permitted there and do not raise the level.
