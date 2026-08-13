# Safety

The rules in CLAUDE.md are stated as absolutes with no exceptions. This document explains why each one earns that status, because a rule whose reasoning is understood survives contact with a deadline and a rule that is merely asserted does not.

The failure mode throughout is the same: **a destroyed driver, usually a tweeter, usually in seconds, usually irreversibly.** Tweeters in a car system are frequently behind trim panels. Replacing one is not a five-minute job.

## Why measurement is more dangerous than music

Music is intermittent and spectrally uneven. A tweeter survives it because the energy above its crossover arrives in short bursts with long gaps.

A measurement sweep is neither. It is **continuous full-amplitude energy that passes slowly through every frequency**, including a sustained period inside the tweeter's most vulnerable range. A level that is unremarkable as music is a thermal overload as a sweep. Voice coils fail from heat, and heat is the time integral of power.

Worse, a sweep sounds quiet while it is doing this. A 100 Hz–20 kHz sweep spends most of its perceptual impact in the midrange; the operator's ear does not warn them about what is happening at 15 kHz.

---

## Rule 1 — Every stimulus passes through `tuner.safety`

> There is no "quick test" exception.

The reasoning is about *when* the mistake happens. A bypass written for a one-off diagnostic is exactly the code that gets copied into a loop, run against an unfamiliar system, or executed at 2 a.m. against the wrong channel.

There is no technical cost to routing through the limiter — it is a scale and a bounds check. A bypass buys nothing and removes the only guard.

Any code path that writes samples to an output device without passing through the limiter is a bug regardless of its purpose.

## Rule 2 — Every sweep starts at −30 dBFS and ramps

> Never jump straight to target level.

The ramp exists because **the first measurement is the one taken with the wrong assumptions.** Before the system is characterized, any of the following may be true and unknown:

- The channel is routed to a different driver than expected.
- Amplifier gain is set far higher than assumed.
- The driver is more efficient than assumed.
- A crossover is bypassed or misconfigured.

All four are discovered by the same mechanism: measure quietly, look at the result, then decide whether to go louder. The ramp makes that the default rather than something the operator has to remember.

−30 dBFS is quiet enough to be harmless into essentially any plausible configuration, and still well above the noise floor for a swept-sine measurement — the method's processing gain is what makes starting this quiet practical.

## Rule 3 — Abort on clipping or DC offset

Both conditions mean **the signal chain is not what the code believes it is.**

- **Clipping** produces harmonics far above the fundamental. A clipped low-frequency signal delivers substantial high-frequency energy directly into the tweeter. This is the classic way an underpowered system destroys tweeters, and a sweep does it deliberately and continuously.
- **DC offset** indicates a fault — a failing coupling capacitor, a mis-set input, a driver already partially damaged. It also displaces the voice coil from its rest position, reducing thermal and mechanical headroom for everything that follows.

Neither is recoverable by adjusting a parameter and continuing. Both invalidate the measurement anyway: a clipped capture's deconvolution is meaningless.

Aborting is not conservative. Continuing past a condition that proves your model of the system is wrong is the actual risk.

## Rule 4 — Uncharacterized channels are treated as tweeters

> Per-channel ceilings are mandatory, and the default is the most conservative one.

Eight outputs on an unfamiliar system are eight unknowns. Something is connected to each; the code does not know what.

The asymmetry decides it:

| Assumption | If right | If wrong |
|---|---|---|
| Treat as tweeter | Correct | Subwoofer measured quietly; repeat louder. Cost: minutes. |
| Treat as woofer | Correct | Tweeter destroyed. Cost: a part and a trim panel. |

`ChannelLimit` enforces this structurally: a channel with `characterized=False` **cannot** have its ceiling raised, even deliberately. Raising it requires first asserting that the driver and crossover are known — which makes the assertion an explicit, reviewable act rather than a forgotten default.

## Rule 5 — Never assume a channel is silent because you did not address it

Routing is a property of the physical system, not of the code's intent. Input mixing on the DSP, a mis-set crossover, a wiring error, or a vendor default can put signal where it was not sent.

**Verify routing by measurement, not by intent** — send a low-level stimulus to one output and confirm which microphone hears it, before trusting any assumption about the channel map.

---

## Operational practice

Beyond what code can enforce:

- **Characterize at low level first.** One quiet sweep per channel to establish what each driver is, before any channel's ceiling is raised.
- **Nobody in the vehicle during high-level sweeps.** Measurement levels sustained across the spectrum are genuinely hazardous to hearing, and the frequencies that do the most damage are the ones that sound least loud.
- **Watch for rattles and mechanical noise.** These indicate excursion limits are being approached — the mechanical analogue of clipping, and a warning the software cannot see.
- **Stop if anything smells hot.** Voice coil failure is thermal and gives warning that instrumentation does not.
- **Repeat suspicious measurements.** Time-variant noise — a door, a passing vehicle, a rattle — corrupts a sweep in ways that are not obvious in the resulting curve.

## Testing

`tests/test_safety.py` asserts that each rule *raises* rather than warning and continuing. That distinction is the point: a warning in a loop is a warning nobody reads.
