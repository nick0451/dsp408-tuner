# The car system: source of truth

**Everything here is operator-declared or datasheet-cited, and each row says
which.** This file exists because tomorrow is the first time this project
drives a system whose drivers can be destroyed by a wrong write, and because
"we discussed it in a session" is not a source.

> ## ⛔ THE DRIVERS HAVE NO PASSIVE CROSSOVERS
>
> Operator, 2026-08-14, verbatim: *"these drivers ARE NOT FILTERED."*
>
> The DSP's crossover is the **only** thing between a full-range signal and a
> 1-inch tweeter. There is no protection capacitor, no passive network, no
> second line of defence. A crossover write that lands wrong does not degrade
> the sound; it ends the driver.
>
> Every other safety rule in this project assumes a mistake is recoverable.
> Here one is not.

## Channels

| out | driver | band now | source |
|---|---|---|---|
| 1, 2 | **FaitalPRO 4FE35**, 4" midrange | 450 – 3500 Hz | operator |
| 3, 4 | **Audiofrog GB10**, 1" tweeter | 3500 – 20000 Hz | operator |
| 5, 6 | **CT Sounds Meso 6.5**, 6.5" midbass | 65 – 450 Hz | operator |
| 7, 8 | subwoofers, two in one ported box | 20 – 65 Hz | operator |

Slopes as read from the device 2026-08-13: **12 dB/oct**, Linkwitz-Riley,
both ends, on every channel (`h_filter`/`l_filter` = 0, `h_level`/`l_level`
= 1, and `slope = 6 × (level + 1)`).

## Driver limits, cited

### Audiofrog GB10 — the one that will die first

| | | grade |
|---|---|---|
| Frequency response | 1.8 – 24 kHz (−3 dB) | manufacturer |
| Power | 100 W RMS, 300 W peak **"with recommended crossover"** | manufacturer |
| **Recommended high-pass** | **≥ 2.5 kHz, ≥ 12 dB/oct** | manufacturer |
| Fs | **not published** | — |

Source: [audiofrog.com GB10](https://www.audiofrog.com/gb10-1-25-mm-audiophile-grade-automotive-tweeter/)

**The power rating is conditional on the filter**, which the manufacturer
states in parentheses on both figures. Below the recommended crossover the
100 W/300 W numbers do not apply and nothing replaces them.

**3500 Hz is 1.4× the stated minimum — good margin. 12 dB/oct is the stated
minimum exactly — no margin at all.**

> ### Why 12 dB/oct is a floor and not a target
>
> A tweeter driven below its resonance sees excursion rising **12 dB/octave**
> as frequency falls, for constant voltage. A 12 dB/octave high-pass exactly
> cancels that, so excursion below the corner is **constant** rather than
> falling — the driver is no longer being protected, only prevented from
> getting worse. At 24 dB/octave excursion *falls* below the corner.
>
> That is the general electro-mechanical argument, not a GB10-specific claim.
> With no passive network behind it, **24 dB/oct is the setting to use.**

### FaitalPRO 4FE35 — small excursion, high Fs

| | | grade |
|---|---|---|
| Fs | **100 Hz** | datasheet aggregator |
| Xmax | **1.73 mm** (Xdam 6.8 mm) | datasheet aggregator |
| Power | 30 W RMS / 60 W peak | vendor listings |
| Sensitivity | 91 dB | vendor listings |
| Usable range | 90 Hz – 20 kHz, flat 90 – 6500 Hz | vendor listings |

Sources: [loudspeakerdatabase](http://www.loudspeakerdatabase.com/Faital/4FE35),
[Parts Express](https://www.parts-express.com/faitalpro-4fe35-4-professional-full-range-woofer-4-ohm--294-1123)

**450 Hz is 4.5× Fs — a comfortable, well-chosen corner.** The 1.73 mm Xmax
is the constraint: this driver has very little travel, so any downward move of
that high-pass costs excursion headroom quickly. **Do not lower it.**

The 3500 Hz low-pass sits inside the flat-to-6500 Hz region, so there is room
to raise it if the tweeter's corner ever moves up. 30 W RMS is modest and
worth remembering before allowing EQ boost on this channel.

### CT Sounds Meso 6.5 — ⚠ the unresolved one

**Which model is not established.** CT Sounds sells several "Meso 6.5":
a 250 W RMS pro-audio midrange (`MESO65-4`), a 160 W RMS component woofer,
a coaxial, and a **subwoofer** whose published Fs is 56.30 Hz and Xmax 8 mm.

Sources: [MESO65-4](https://www.ctsounds.com/products/meso65-4),
[MESO-6-5-COM](https://www.ctsounds.com/products/meso-6-5-com),
[MESO-6-5 subwoofer](https://www.ctsounds.com/products/meso-6-5)

**This matters because the channel is high-passed at 65 Hz.** If this driver's
Fs is anywhere near the subwoofer variant's 56 Hz, then a 65 Hz corner sits
essentially *on resonance* — which is exactly where excursion peaks and where
a 12 dB/oct filter provides the least protection. A 6.5" driven hard at 65 Hz
with a 12 dB/oct corner is a plausible way to exceed Xmax.

**Operator question, before raising any level on channels 5/6: which Meso
6.5 is installed, and what is its Fs and Xmax?** Until answered, treat 65 Hz
as the lowest corner permitted on that channel and do not lower it.

### Subwoofers — unknown, and ported

Two drivers in one **ported** enclosure (operator, 2026-08-10), which is why
`orchestrate.plan.Gang` exists: box pressure is common to both cones, so
driving them unequally back-drives one of them.

**Below port tuning a ported box unloads and excursion runs away.** The
20 Hz high-pass is the subsonic filter that prevents that, and **12 dB/oct is
light for the job — 24 dB/oct is the usual choice.** Model, Fs, Xmax and port
tuning are all unknown; the tuning frequency is what decides where the
subsonic corner belongs.

## Clip limits — operator-declared, 2026-08-14

| control | clips above | tune at | scale |
|---|---|---|---|
| Master gain | **54** | **48** | iOS app |
| Input gain | **90** | — | iOS app, 0–100 |
| Output channel gain | **54** | — | iOS app, 0–60 |
| REW sweep level | — | **−12 dBFS** | dBFS |

### ✅ The output-gain scale is confirmed: **iOS display = dB + 60**

Operator-confirmed 2026-08-14, and it agrees with `protocol.gain_dbfs`:

| iOS | `gain_raw` | dB |
|---|---|---|
| 60 | 600 | **0.00** |
| **54** | 540 | **−6.00** ← output channels clip above here |
| 50 | 500 | −10.00 |
| **48** | 480 | **−12.00** ← the tuning position |
| 0 | 0 | −60.00 |

So **"output channels clip past 54" means keep output gain at or below
−6.0 dB**, and that is now a number code may enforce.

> **One correction worth making precisely, because it is a clip limit.** The
> operator's recollection paired 50 with −12 dB. 50 is **−10.00 dB**; −12.00 dB
> is **48**. The 2 dB matters, and this particular value carries the strongest
> evidence in the project: `gain_raw` 500 was read out of the **ADAU1701's own
> coefficient memory** as exactly −10.0000 dB in 5.23 format on 2026-08-11 —
> not inferred from a display but read from the silicon, with the Android
> display and a measured 6.00 dB step agreeing independently.
>
> It is also self-consistent with the operator's own instruction to tune with
> master gain at **48**, which is the −12 dB position.

**Master gain is a different parameter** from output channel gain, and its
scale is not established here. 54 and 48 are recorded as the operator's
app-scale numbers; nothing is inferred from them.

**Input gain lives in `DataType 3`, which no protocol work has ever reached**
— the Android captures never touched it, and the iOS app is the only known
vendor path to it. 90 is a limit we can respect operationally and cannot read
or write.

## Delay: the anchor, and why there are two of them

This project already separates two roles that get conflated; see
[time-alignment.md](time-alignment.md).

| | **Timing reference** | **Alignment anchor** |
|---|---|---|
| Chosen for | **detectability** — a sharp arrival | **physics** — a DSP can only *add* delay |
| Which | a **tweeter**; the reference sweep is 5–20 kHz, so a sub cannot serve | the driver **farthest** from the seat, which gets 0 ms |
| Here | **CH3 or CH4, the GB10** | **measure it** |

### Recommended: passenger-side GB10 as the timing reference

It covers 1.8–24 kHz, so it passes the whole reference band, and it is the
sharpest arrival in the system. Two obligations follow and both are real:

1. **Its delay and crossover must be frozen for the entire run.** The
   reference chirp passes through that DSP channel, so the channel's delay is
   inside the time origin. Write a delay to it mid-run and t=0 moves for every
   measurement after, by exactly that amount, looking like an acoustic change.
2. **The chirp is a stimulus** and needs a `DriverCeiling` with a basis like
   any other — it fires twice per measurement, making it the most frequently
   emitted signal in the run.

### The anchor is measured, never assumed

The alignment anchor is whichever driver **arrives latest**, because delay can
only be added. Everything else is delayed forward to meet it.

**It is usually the subwoofer and not always**, and `CLAUDE.md` says in as
many words: never hardcode it. In a driver's-seat tune the passenger-side
drivers are farther than the driver's-side ones, and a boot-mounted sub is
usually farthest of all — but "usually" is how a tune ends up aligned to the
wrong reference.

REW's timing reference supports **`Acoustic`**, confirmed present in the API's
`/measure/timing/reference/choices`. That is the mechanism: every measurement
in the session refers to the same acoustic event through the same reference
speaker, so arrivals become comparable *to each other*. Absolute delay stays
unavailable and is refused — the reference speaker's own path length is an
unmeasured constant inside every arrival.

## Recommended changes, and the argument for each

None of these has been made. Each needs the operator's agreement.

| change | from | to | why |
|---|---|---|---|
| CH3/4 high-pass slope | 12 | **24 dB/oct** | 12 is the GB10's stated *minimum*; at 12 dB/oct excursion below the corner is constant rather than falling, and there is no passive network behind it |
| CH7/8 subsonic slope | 12 | **24 dB/oct** | below port tuning a ported box unloads and excursion runs away |
| CH1/2 high-pass | 450 | **leave** | 4.5× Fs with 1.73 mm of Xmax; there is nothing to gain and excursion to lose |
| CH5/6 high-pass | 65 | **leave until Fs is known** | 65 Hz may sit on resonance |

> ### ⚠ Changing a crossover type changes the voltage at the corner
>
> Linkwitz-Riley is −6 dB at the corner and its two halves sum **flat**.
> Butterworth is −3 dB and sums **+3 dB**. So switching CH3/4 from LR to
> Butterworth does not merely re-shape the transition — it puts **3 dB more
> voltage** into both the tweeter and the midrange right where they overlap.
>
> `ChannelConfig` cannot express the filter type at all: `Dsp408Spp` carries
> `h_filter`/`l_filter` through unchanged from the stored record. So this
> project **cannot** make that mistake through its own backend, only through
> the vendor app. Worth knowing which of those is true rather than assuming
> the safety comes from care.

## ⛔ Tools that are unsafe on this system as written

**`tools/bench_flatten.py` opens crossovers to 20 Hz – 20 kHz.** That is
correct on the bench, where OUT1/2 feed a plate amp that does its own
splitting. On this car it would feed **20 Hz to unprotected 1" tweeters** on
CH3/4 and destroy them.

Guarded 2026-08-14: the tool now refuses to widen the corners of any channel
whose existing high-pass is at or above `PROTECTED_HIGH_PASS_HZ`, on the
reasoning that a channel filtered that high is filtered that high *for a
reason*. Overriding it takes a written basis.
