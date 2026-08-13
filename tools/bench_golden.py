"""Produce our half of the REW golden, and print the instructions for theirs.

**The debt this pays.** ``CLAUDE.md``'s validation policy once asserted that
the measurement engine was validated against REW, with reference data in
``tests/golden/``. No such test was ever written and no such data existed. What
does exist is analytic known-answer tests -- real, but self-referential -- and
an electrical loopback, which is our chain measuring our chain. Neither is
independent ground truth, and a deconvolution bug does not announce itself: it
produces a smooth, plausible curve that is simply wrong.

**The method.** Both tools measure the *same physical path*, one after the
other, with nothing touched in between. REW generates its own sweep, does its
own deconvolution, its own windowing and its own FFT; it shares not one line of
code with us. That is what makes the comparison worth running. Driving REW by
hand rather than through its REST API costs clicks and changes nothing about
the independence -- the API is a paid feature and buys no validity here.

**Why the device under test has features.** A bare loopback is nearly flat, and
two implementations agreeing that a straight line is straight validates almost
nothing. The channel is configured with a crossover and a deep narrow cut, so
there are steep slopes and fast phase rotation -- which is where windowing and
deconvolution errors actually show up.

**Why a cut and not a boost.** A boost applied inside the DSP is downstream of
everything ``tuner.safety`` can see, and it would force both tools to run below
their comfortable levels to keep headroom. A cut needs none, so both tools can
run where their signal-to-noise is best and neither has a clipping trap.

Run this, then follow the printed REW instructions, then run
``pytest tests/test_golden_rew.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tuner.measure import (  # noqa: E402
    CaptureConfig,
    SessionInfo,
    capture_sweep,
    log_freqs,
    measure_level_linearity,
    require_linear_path,
)
from tuner.safety import ChannelLimit  # noqa: E402

#: Host APIs the Scarlett is exposed under. Selected by **name**, never index:
#: MME lists the host's default output first, so indices renumber when the
#: default changes, and on this bench that once pointed a measurement at the
#: PC's speakers while still capturing the correct input.
#:
#: **Measured 2026-08-09: use WASAPI. MME was a large part of the HF artifact.**
#:
#: Same DUT, same level, same session, back-to-back runs. Run-to-run scatter,
#: dB max:
#:
#: =============  =====  ======  =======
#: Band              MME  WASAPI   WDM-KS
#: =============  =====  ======  =======
#: 30-250 Hz      0.144   0.054    0.124
#: 250-1000       0.515   0.231    0.267
#: 1000-2000      0.260   0.315    0.251
#: 2000-3500      0.245   0.132    0.296
#: 3500-5000      0.317   0.091    0.251
#: 5000-7000      1.772   0.384    0.352
#: 7000-10000     1.418   0.792    1.504
#: 10000-14000    4.289  11.679   21.740
#: =============  =====  ======  =======
#:
#: MME is 2-5x worse from 250 Hz to 10 kHz, at identical levels -- 4.6x at
#: 5-7 kHz, where the signal is a comfortable -33 dBFS, so this is not a
#: signal-to-noise story. WDM-KS bypasses the Windows mixer and so ran 30 dB
#: quieter; its numbers are not comparable and it was not pursued.
#:
#: **The measured curve is unchanged**: WASAPI against MME agrees to 0.236 dB
#: max and 0.058 dB rms over the passband. That known-answer check is what
#: licenses the switch -- a host API that changed the *answer* would invalidate
#: every earlier measurement, whereas one that only changes the *scatter* leaves
#: them comparable, well inside the 0.39 dB repeatability floor.
#:
#: Above 10 kHz nothing helps, but on this DUT the low-pass leaves nothing there
#: to measure. Earlier flat-channel runs with real HF content repeated to
#: ~0.4 dB at 14.5 kHz under MME, so **no claim is made about >10 kHz** with
#: signal present.
#:
#: Selected by **name**, never index: MME lists the host's default output first,
#: so indices renumber when the default changes, and on this bench that once
#: pointed a measurement at the PC's speakers while still capturing the correct
#: input.
HOST_APIS = ("Windows WASAPI", "MME", "Windows WDM-KS", "Windows DirectSound")

SAMPLE_RATE_HZ = 44_100

#: The configuration the golden is taken against. Stored alongside the data,
#: because a reference measured on an unrecorded system is not a reference.
DUT = {
    "output": "OUT1",
    "highpass_hz": 20,
    "lowpass_hz": 3500,
    "eq_band": "app band 2 (eq[1])",
    "eq_freq_hz": 1000,
    "eq_gain_db": -12.0,
    "eq_bw_raw": 65,
    "eq_q": 2.041,
    "other_bands": "flat, level_raw 600",
}

#: Compared over the **device's passband**, which is the only band where the
#: comparison means anything.
#:
#: Was 30 Hz - 18 kHz until 2026-08-09. That was an error in test design, not a
#: judgement about the data: the DUT is low-passed at 3.5 kHz, so a band
#: reaching 18 kHz spends 2.4 octaves comparing two measurements of a signal
#: the device has deliberately removed. Measured there, our own run-to-run
#: repeatability is **7 dB**, larger than our disagreement with REW -- an
#: instrument cannot be checked against a reference in a band where it does not
#: agree with itself.
#:
#: The tolerance did not move. See ``tests/test_golden_rew.py``.
COMPARE_LO_HZ = 30.0
COMPARE_HI_HZ = 3_500.0

REW_INSTRUCTIONS = """
================================ REW: do this ================================

Nothing here may change the electrical path. Do not touch the Scarlett knobs,
do not unplug anything, do not reconfigure the DSP. The whole comparison rests
on both tools seeing the same system.

1. Preferences -> Soundcard
     Output device : Speakers (Scarlett Solo USB)      [driver: Java/MME]
     Output         : left / channel 1
     Input device  : Microphone (Scarlett Solo USB)
     Input          : right / channel 2   (the 1/4" jack)
     Sample rate   : 44100
     **No calibration files.** Clear any soundcard cal and any mic cal. A cal
     file would make REW correct for something we are not correcting for, and
     the disagreement would be the cal, not the engine.

2. Preferences -> Analysis
     **Smoothing: None.** REW smooths for display by default; a smoothed
     reference would hide exactly the narrow features this test exists to
     compare.
     Leave "Remove time delay" and any windowing at their defaults, and tell
     me what they were -- an electrical loopback has no reflections, so the
     window should not matter, and if it does that is itself the finding.

3. Measure
     Type          : Sweep
     Range         : 20 Hz to 20 kHz
     Length        : 256k or longer
     Level         : -12.0 dBFS
     Repetitions   : 1 is fine; more is better
     Do NOT enable "Use acoustic timing reference".

4. Export
     File -> Export -> Measurement as text
     Save as:  {out}/rew_response.txt
     Tick "Use REW export format" if offered; leave the frequency range as
     measured. What we need is frequency and SPL columns, unsmoothed.

Then run:  pytest tests/test_golden_rew.py -v
==============================================================================
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests" / "golden" / "rew",
    )
    ap.add_argument("--level-dbfs", type=float, default=-12.0)
    ap.add_argument(
        "--electrical-only",
        action="store_true",
        help="assert nothing but a line input is on this output",
    )
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--points", type=int, default=600)
    ap.add_argument("--temperature-c", type=float, default=None)
    ap.add_argument("--note", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument(
        "--host-api",
        default="Windows WASAPI",
        choices=HOST_APIS,
        help="PortAudio host API to select the Scarlett under, by name. "
        "WASAPI by measurement, not by preference -- see HOST_APIS.",
    )
    args = ap.parse_args()

    output_device = f"Speakers (Scarlett Solo USB), {args.host_api}"
    input_device = f"Microphone (Scarlett Solo USB), {args.host_api}"

    if args.electrical_only:
        limit = ChannelLimit(ceiling_dbfs=args.level_dbfs, characterized=True)
    else:
        limit = ChannelLimit()
        if args.level_dbfs > limit.ceiling_dbfs:
            ap.error(
                f"level {args.level_dbfs} dBFS exceeds the default "
                f"{limit.ceiling_dbfs} dBFS ceiling. Pass --electrical-only "
                f"only if nothing but a line input is on this output."
            )

    device = (input_device, output_device)
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"Host API       {args.host_api}")
    print("Device under test")
    for key, value in DUT.items():
        print(f"  {key:<14} {value}")

    print("\nLevel linearity (~14 s) ...")
    result = measure_level_linearity(
        sample_rate_hz=SAMPLE_RATE_HZ,
        output_channel=0,
        input_channel=1,
        device=device,
        limit=limit,
        # Inside the channel's 20-3500 Hz passband, and clear of the 1 kHz cut
        # so the tones are not sitting in a 12 dB hole.
        freqs_hz=(100.0, 300.0, 2000.0),
    )
    require_linear_path(result)
    print(f"  gain spread {result.spread_db:.2f} dB across level -- linear.")

    notes = dict(n.split("=", 1) for n in args.note)
    notes.setdefault("purpose", "REW golden: independent ground truth for the engine")
    config = CaptureConfig(
        sample_rate_hz=SAMPLE_RATE_HZ,
        device=device,
        output_channel=0,
        input_channels=(1,),
        level_dbfs=args.level_dbfs,
        limit=limit,
        repeats=args.repeats,
    )
    session = SessionInfo(
        gains_db=(0.0,), temperature_c=args.temperature_c, notes=notes
    )

    freqs = log_freqs(10.0, 20_000.0, args.points)

    def sweep_and_write(filename: str):
        """One full measurement, written where the golden test can find it."""
        measured = capture_sweep(config, session)[1]
        magnitude_dbfs = measured.magnitude_dbfs(freqs)
        path = args.out / filename
        with path.open("w", encoding="utf-8") as fh:
            fh.write("# tuner frequency response, magnitude only\n")
            fh.write(f"# sample_rate_hz {SAMPLE_RATE_HZ}\n")
            fh.write(f"# level_dbfs {args.level_dbfs}\n")
            fh.write(f"# repeats {args.repeats}\n")
            fh.write("# freq_hz  magnitude_dbfs\n")
            for f, m in zip(freqs, magnitude_dbfs, strict=True):
                fh.write(f"{f:.4f}  {m:.5f}\n")
        return measured, magnitude_dbfs, path

    # **Two independent measurements, not one.**
    #
    # The second is not redundancy, it is the evidence that the comparison band
    # is a band worth comparing in. Our repeatability collapses to 7 dB where
    # the DUT has attenuated the signal by 50 dB, and a reference comparison
    # run there measures our own noise. Shipping the repeat alongside the
    # golden lets the test *prove* the band is valid instead of asserting it,
    # and makes any future widening fail honestly.
    print(f"\nSweep A, median of {args.repeats} ...")
    measurement, magnitude, response = sweep_and_write("ours_response.txt")
    print(f"Sweep B (repeatability), median of {args.repeats} ...")
    _, repeat, _ = sweep_and_write("ours_repeat.txt")

    np.save(args.out / "ours_impulse.npy", measurement.impulse)

    prov = measurement.provenance
    (args.out / "provenance.json").write_text(
        json.dumps(
            {
                "dut": DUT,
                "device": prov.device,
                "host_api": args.host_api,
                "sample_rate_hz": prov.sample_rate_hz,
                "timestamp": prov.timestamp.isoformat(),
                "level_dbfs": args.level_dbfs,
                "repeats": args.repeats,
                "points": args.points,
                "compare_band_hz": [COMPARE_LO_HZ, COMPARE_HI_HZ],
                "notes": notes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    band = (freqs >= COMPARE_LO_HZ) & (freqs <= COMPARE_HI_HZ)
    scatter = np.abs(magnitude - repeat)
    print(f"\n  wrote {response} and ours_repeat.txt")
    print(
        f"  {band.sum()} points in the {COMPARE_LO_HZ:.0f}-{COMPARE_HI_HZ:.0f} Hz "
        f"comparison band"
    )
    print(
        f"  our curve spans {magnitude[band].min():.2f} to "
        f"{magnitude[band].max():.2f} dBFS"
    )
    print(
        f"  our own A-vs-B scatter: {scatter[band].max():.3f} dB max in band, "
        f"{scatter.max():.3f} dB over the full 10 Hz-20 kHz sweep"
    )
    print(REW_INSTRUCTIONS.format(out=args.out.as_posix()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
