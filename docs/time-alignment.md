# How car audio actually sets time alignment

Research, 2026-08-13, scoped deliberately to one question: **how practitioners
establish a time origin in a car and decide which driver everything else is
aligned to.** Not tuning theory, not target curves, not crossover design.

The headline is that this project has been conflating two things the field
keeps separate, and they are chosen by opposite criteria.

---

## The two references are different drivers

| | **Timing reference** | **Alignment reference** |
|---|---|---|
| What it is | The speaker that plays the chirp establishing t=0 for a measurement | The driver that gets **0 ms** of DSP delay; everything else is delayed to meet it |
| Chosen for | **Detectability** — a sharp, unambiguous arrival | **Physics** — a DSP can only *add* delay |
| Which driver | A tweeter. REW's reference is a **5–20 kHz linear sweep**, so a subwoofer physically cannot serve | The **farthest** driver from the listening position |
| Typically | A front tweeter, often the near one | Usually but *not always* the subwoofer |
| Changes during a run? | **Never** | It is an output of the measurement |

Our `TimingReference.ACOUSTIC` implements the first. `normalize_delays`
already implements the second, by subtracting the common minimum — the driver
needing the least DSP delay ends at zero, and that driver is the farthest one
by construction. **So the two halves exist; what was missing is that they are
separate roles and the plan has to name the first one explicitly.**

### Why the farthest driver, and why it is not always the sub

Delay can only be added. The farthest driver's sound already arrives last, so
nothing can be done to bring it forward — every other driver waits for it.
Reversing this collapses the soundstage toward the nearest speaker.

The field's default assumption is that the farthest driver is the subwoofer,
and the field also explicitly warns against hardcoding that: *"Sometimes the
right side of the front stage can be physically further away than the
subwoofer is, so there is no fast rule that says the sub should have zero
delay."* Derive it from the measurement, never from the layout.

---

## What the field says goes wrong, and what we already do about it

### ⚠ The acoustic timing reference is *known to be unreliable in a car*

REW's author, John Mulcahy, on the record: using the acoustic timing reference
in a car **"can be problematic as there are many strong, close reflections
which can affect the determination of the reference time"**, and in a highly
reflective environment *"detection of the timing signal arrival can be
unreliable and is not likely to give good timing results."*

This is the single most important finding, and it is aimed squarely at the
architecture we built yesterday. Three things follow.

**First, it is an argument for the wide band, not against the method.** The
correlation peak's width is set by the reference's bandwidth, and that width
is exactly what decides whether a direct arrival can be told apart from a
reflection. 5–20 kHz resolves arrivals ~200 µs apart (69 mm); the 2–8 kHz band
we shipped yesterday resolved only ~500 µs (172 mm). In a cabin, where the
first reflection is often under a millisecond behind, that difference decides
whether the method works at all. **Changed: `REFERENCE_START_HZ`/`STOP_HZ` are
now 5 kHz / 20 kHz**, matching REW.

**Second, a consistently wrong detection is survivable and an inconsistent one
is not.** From the forums: if the detector locks onto a reflection, *"the
error may be consistently the same amount"*. That maps exactly onto what we
built — we refuse absolute delay and report only relative, so a constant error
common to every measurement **cancels**. What does not cancel is a detector
that switches between peaks from measurement to measurement.

> **So the guard that matters is repeatability of the arrival, not its
> correctness.** Across the repeats of one measurement the reference arrival
> should not move. If it jumps, every delay derived from that session is
> unreliable, and no amount of averaging will say so. *Not yet built.*

**Third, our first-group rule is the right shape.** We take the first peak
group rather than the largest, which is what stops a louder reflection being
reported as t=0. The field hit exactly this failure: *"If REW identifies the
IR peak and there is a reflection, it might use the wrong reference point."*

### ⚠ Crossovers on the reference channel corrupt detection

From DIYMA: *"crossovers should be disabled on the DSP, as enabled crossovers
can change phase and cause readings to not be detected at the right moment"*,
and *"leaving filters like HPF enabled on tweeters can cause issues with
impulse detection as the crossover affects phase."*

This is stronger than the rule we wrote yesterday. We said *freeze* the
reference channel's delay and crossover, on the grounds that changing it moves
t=0 by a constant. The field says an enabled crossover degrades the
*detection* itself, not just its offset.

Both are true and they need different responses:

- **Freezing** is what makes the constant cancel. Keep it, as a plan-level
  refusal.
- **Detection quality** is why the reference band should sit well above the
  reference tweeter's high-pass corner. A 3.5 kHz crossover under a 5–20 kHz
  chirp is fine; the same crossover under a 2–8 kHz chirp puts a third of the
  reference's energy inside the filter's transition band. **Another reason
  the band change was right.**

### ⚠ A narrow-bandwidth driver's impulse peak is a poor arrival estimate

*"If drivers have significantly different bandwidth, the IR peak location
estimates may be less accurate for the narrower-bandwidth driver."*

This is the subwoofer problem, and it is already a rule in `CLAUDE.md` —
cross-correlating one driver against another fails hardest on the pair that
matters. The acoustic reference solves the *time-origin* half of it. It does
not make a subwoofer's impulse peak a good number.

### The null is a sharper target than the peak

The field's method for sub-to-midbass polarity: **invert the sub's polarity,
re-measure, and choose the polarity that produces the *deepest null* at the
crossover** — then use the opposite one. The null is the sharp feature; the
summation peak is broad and hard to locate.

That is a better optimisation target than "maximise coherent summation", and
it is the same reasoning this project already uses for differential
measurement: find the feature with the steep gradient, not the flat one.

### Microphone placement

For measuring the delay *between* two drivers, the field warns that having the
microphone at tweeter level gives an incorrect reading; put it vertically
midway between the two drivers, **or at the listening position**. For a
whole-car tune the listening position is the answer, since that is where the
objective is defined anyway.

---

## The algorithm this suggests

```
SESSION SETUP (once, declared in the plan)
  1. Nominate a timing_reference_output.
       - must be a tweeter or a driver flat across 5-20 kHz
       - must NOT be the subwoofer
       - its delay and crossover are FROZEN for the whole run
       - it needs a DriverCeiling with a basis, like any stimulus, and it
         should be the most conservative one in the run: it fires twice per
         measurement, every measurement
  2. Declare the setup token. It now guards timing, not just magnitude.

PER MEASUREMENT
  3. REF_A -> gap -> sweep the driver under test -> gap -> REF_B
       gap >= the cabin's decay, or the chirp's tail smears into the sweep
  4. Detect both references (first peak group, not the largest)
  5. clock_ratio = captured interval / generated interval; correct the
     capture's timebase
  6. Deconvolve. t=0 is REF_A. Do NOT peak-align the IR independently --
     that discards the timing the reference just established
  7. GUARD: the REF_A arrival must repeat across the measurement's repeats.
     If it moves, the detector is switching peaks; refuse the session

DERIVING DELAYS
  8. Every driver's arrival is now on one time origin, including the sub,
     because the reference carried the timing rather than the sub
  9. alignment_reference = argmax(arrival)   <- the farthest driver, measured
       never assumed to be the sub
 10. dsp_delay[i] = arrival[reference] - arrival[i]        (>= 0 by choice of
     reference; this is what normalize_delays already produces)

CROSSOVER PAIRS (mids/tweeters, and especially sub/midbass)
 11. Do not trust the sub's impulse peak. With a shared time origin, compare
     complex transfer functions over the crossover window instead
 12. Polarity by the null: invert, re-measure, take the polarity that gives
     the DEEPER null, then use the opposite one
```

Steps 1, 3–7 and 11–12 are not built. Steps 8–10 are, modulo the reference
being named in the plan.

---

## What this changed today

- **Reference band 2–8 kHz → 5–20 kHz.** Matches REW, resolves arrivals 2.5×
  finer (200 µs / 69 mm), and sits clear of a tweeter's high-pass corner.
- **A test that had been passing for the wrong reason.** The sub-sample
  assertion pinned a *sign* nobody had derived, and a three-point parabolic
  fit on the old wide lobe was inaccurate enough to report the wrong
  direction. Sharpening the peak broke it. The claim is now scoped to what is
  demonstrated: one sample of arrival accuracy, and 10 ppm of clock ratio end
  to end.

## Still to decide

- **Whether the timing reference should be the near tweeter or the far one.**
  The field says "a tweeter" and one report says switching to the left tweeter
  made everything consistent, but nobody gives a rule. Nearer means a stronger
  direct-to-reflection ratio at the mic, which argues for near.
- **Whether to reuse the alignment reference as the timing reference.** They
  are different roles, and the farthest driver is often the sub, which cannot
  be the timing reference at all. So: usually not.

## Sources

- [minidsp — Measuring time delay with REW](https://www.minidsp.com/applications/rew/measuring-time-delay)
- [AV NIRVANA — Car audio time alignment using REW?](https://www.avnirvana.com/threads/car-audio-time-alignment-using-rew.11281/)
- [AV NIRVANA — Reference speaker when using acoustic timing reference](https://www.avnirvana.com/threads/reference-speaker-when-using-acoustic-timing-reference-to-measure-delay.2099/)
- [Audio Intensity — REW car audio guide](https://audiointensity.com/blogs/how-to-test/rew-car-audio-beginner-to-advanced)
- [Audio Intensity — Car audio time alignment setup guide](https://audiointensity.com/blogs/dsp/car-audio-time-alignment)
- [DIYMobileAudio — Timing reference added in REW](https://www.diymobileaudio.com/threads/timing-reference-added-in-rew.259482/)
- [DIYMobileAudio — Acoustical timing reference inconsistent](https://www.diymobileaudio.com/threads/acoustical-timing-reference-delay-for-setting-time-alignment-inconsistant.467536/)
- [DIYMobileAudio — Time correction philosophy](https://www.diymobileaudio.com/threads/time-correction-philosophy.169915/)
- [diyAudio — Acoustic timing reference for driver time alignment in REW](https://www.diyaudio.com/community/threads/acoustic-timing-reference-for-driver-time-alignement-in-rew.378698/)
- [Audiofrog — Time Alignment Part 1](https://www.audiofrog.com/time-alignment-part-1/)
- [HouseCurve — Time alignment](https://housecurve.com/docs/tuning/time_align)
