# OUT1 state before the 2026-08-13 bench flatten

**Why this file exists.** `bench_flatten.py` captured its snapshot *before*
writing, which is right, and saved it to a fixed path, which was not. The tool
was run three times against the same path: a dry run, an attempt that wrote
OUT1 and was then stopped by the blast-radius cap, and a third that completed.
The third captured a device on which **OUT1 was already flat**, and overwrote
the pristine file with it.

`snapshots/pre-bench-2026-08-13.json` (digest `d37822ac2fb0fad4`) therefore
holds **OUT1 already flattened and OUT2 pristine**. OUT2 restores from it
exactly. OUT1 does not, and these are its values, read off the device and
printed twice before anything was written:

| | |
|---|---|
| gain | **−10.00 dB** (`gain_raw` 500) |
| delay | **144 samples** |
| crossover | **450 – 3500 Hz**, Linkwitz-Riley, **12 dB/oct** (`h_filter`/`l_filter` 0, `h_level`/`l_level` 1) |
| EQ band 1 | **2514 Hz, −12.00 dB, Q 3.056** (`bw_raw` 42 → 0.47 octaves) |
| EQ bands 2–31 | flat |
| muted | no |

Raw XOVER block as read: `OutputXover(h_freq=450, h_filter=0, h_level=1,
l_freq=3500, l_filter=0, l_level=1)`.

**The single EQ band is this project's own M4 known-answer perturbation**, not
part of the car's tune — `CLAUDE.md` records it as `2514 Hz, −12.00 dB, bw 42`
from the 2026-08-12 closed-loop run. So OUT1's "original" state was already
bench state rather than the operator's, which lowers the cost of this mistake
without excusing it.

The operator's own fallback is preset slot 4, `lbass`, which is untouched.

## The fix

`bench_flatten.py` now **refuses to overwrite an existing snapshot path**. A
restore point that a re-run can silently replace is not a restore point, and
the failure mode is specifically nasty: the second capture looks healthy, has
a valid digest, and restores the device to a state nobody asked for.
