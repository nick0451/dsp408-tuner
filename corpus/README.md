# The `.DDP` corpus

Tune backups saved from the vendor app, off a real Dayton Audio DSP-408.
These are **evidence, not fixtures**. Several decoded fields rest on the A/Bs
among them, and the tests read them directly rather than from anything this
project generated.

A `.DDP` is `49-byte header + 553 × 8-byte blocks`; see
[`src/tuner/dsp/ddp.py`](../src/tuner/dsp/ddp.py) for the layout and
[`tools/ddp_dump.py`](../tools/ddp_dump.py) to diff two of them:

```
python tools/ddp_dump.py corpus/before.DDP corpus/after.DDP
```

## Why single-control A/Bs

Most of these files differ from a sibling in exactly one app control. That is
the whole method: **when a byte reads the same in every file you have, the
field is not opaque — your evidence is exhausted.**

Three fields read as constants across all 112 channel-records in the original
backups and were assumed unmappable. They were not. The corpus simply had no
variation in them, because every tune ever saved was Linkwitz-Riley at 12 or
24 dB/octave with no shelves. Fourteen deliberate single-control saves settled
what no further analysis of the existing files could have.

| Group | What one control was moved between saves |
|---|---|
| `dspcartunebackups_c1_hpf*` / `c1_lpf*` | Crossover slope, 6 through 24 dB/octave — gave `slope = 6 × (level + 1)` |
| `..._bessel` / `_butterworth` / `_defeat` | Crossover alignment — gave 0 Linkwitz-Riley / 1 Butterworth / 2 Bessel / 3 Defeat |
| `c1_ls_en` / `c1_hs_en` / `c1_ls_and_hs_en` | EQ band type — gave 0 PEQ / 1 low shelf / 2 high shelf |
| `d1_bw*` / `d1_lf*` | EQ bandwidth and frequency, against measured response |
| `eq_channel1_mute` vs `..._no_mute` | Mute, which changed exactly one byte in the whole file with `gain_raw` untouched |
| `eq_1_baseline` … `eq_5_restore` | The app's bypass/reset/restore ladder, which distinguishes bypass from reset by byte signature |

## What they are used for

- `tests/test_ddp.py` — the reader, and the A/B conclusions above
- `tests/test_bulk_record.py` — **cross-validation**: our RFCOMM readback of
  the device's 296-byte channel records equals the output section of these
  files, byte for byte, 2368 bytes. Two paths sharing no code, answering the
  same question
- `tests/test_snapshot.py` — splicing a snapshot back into a `.DDP` the vendor
  app can load, which is the restore route that survives everything else
  failing

## A caution

These are one operator's real tunes off one unit. Do not read a value here as
a device default or as a factory setting — several were captured mid-tune, and
at least one file pair was taken specifically to record a *damaged* state so
that a restore could be verified against it.
