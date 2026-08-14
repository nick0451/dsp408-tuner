# REW cross-reference, bench 2.1, 2026-08-13

Five identical sweeps taken to answer one question: **is the instability our
measurement engine, or the rig?**

Our own tone probe was scattering 0.6–1.9 dB standard deviation on repeated
identical readings, which is enough to make `require_linear_path`'s 1.0 dB
tolerance meaningless. Ruling the estimator out took one experiment (four
integration widths on the same captures agreed to 0.07 dB), but that only
established the acoustic level was genuinely moving — not why.

REW shares no code with this project. That is the entire reason to ask it.

## Conditions

| | |
|---|---|
| REW | 5.31.3 |
| Output | Scarlett Solo, **WASAPI exclusive**, channel L |
| Input | UMIK-1, **WASAPI exclusive**, channel R, volume 0.540 |
| Rate | 48 kHz both ends |
| Stimulus | 256k log swept sine, **1 sweep**, −12.0 dBFS |
| Timing reference | **none** — no loopback is possible across two clocks |
| Averaging | **none** |
| Microphone calibration | **not loaded** |
| DSP | OUT1, 450–3500 Hz, gain −10 dB, carrying `2514 Hz / −12 dB / Q 3.06` |
| Speakers | Logitech THX 2.1, plate amp, DSP OUT1+2 to its line inputs |

Two of those are deliberate and easy to get wrong.

**No averaging, because there is no timing reference.** Asked to average 8
sweeps without one, REW produced 62 dB of comb filtering on this rig — kept
as `tests/golden/rew/flat_rew_8sweeps_unaligned.txt`. More sweeps is worse
than one here.

**No calibration file, because our engine applies none.** Loading one would
have compared the cal file rather than the rig. It costs little in the band
of interest: the UMIK's curve is within ±0.25 dB below 3.5 kHz, rising to
+2.82 dB at 10 kHz.

The stimulus level is −12.0 dBFS where our own runs use −20.0. That was not
matched, and it makes the comparison *favourable* to REW — it had 8 dB more
signal-to-noise than we did and was still noisier.

## Result

| 450–3500 Hz | |
|---|---|
| median spread across the five | **3.90 dB** |
| max spread | 24.93 dB |
| worst pairwise rms | 4.62 dB |
| band mean drift across the five | **0.51 dB** |

The band mean barely moves while the spread is large, and level-matching does
not reduce it (3.90 → 3.87 dB). So this is **shape**, not level: nothing is
warming up or drifting in gain.

The movement is confined to the nulls of a comb:

| | mean vs peak | spread |
|---|---|---|
| 856 Hz | −2 dB | **0.8 dB** |
| 1196 Hz | −6 dB | **0.8 dB** |
| 1380 Hz | −12 dB | **20.5 dB** |
| 1518 Hz | −20 dB | **18.7 dB** |
| 4334 Hz | −23 dB | **22.9 dB** |

Null spacing is roughly 150 Hz, putting the reflection about **2.3 m** longer
than the direct path — a room boundary rather than the desk edge.

This retro-explains the whole session: the linearity tones that misbehaved,
720 and 1600 Hz, sit beside nulls at 675 and 1592/1670. The two that behaved,
900 and 1255 Hz, are in smooth regions. **The linearity check was measuring
the room's comb, not the amplifier.**

## Why the raw files are here at 9.3 MB

A downsample to 500 log points was tried first and rejected by its own
verification: the aggregate statistics survived (median 3.90 → 3.67 dB) but
individual points differed by up to **22 dB**, because a small frequency
shift near a sharp null is a large level change. `docs/STATE.md` quotes
per-frequency numbers from these files, and evidence that does not reproduce
at the resolution it is quoted at is not evidence.

`L Aug 13.txt` is a sixth capture taken before the set of five, kept as it
was exported.

## Superseded

These were taken with the microphone at its original position. The action
they prompted was to move it near-field, 20–30 cm on axis and off the desk
surface, and repeat. Keep this set: it is the measurement that settled the
diagnosis, and the near-field set is only interpretable against it.
