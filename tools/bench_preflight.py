"""Everything that must be true before a bench tune, checked rather than assumed.

    python tools/bench_preflight.py --address <MAC> --outputs 1 2

**Written because a routing assumption cost a listening test.** On
2026-08-13 a tune was fitted, written and A/B'd on the stated belief that
"Apple Music will send R to IN2 to OUT2". OUT2's mixer took IN2 and IN4 and
only IN1 was cabled, so the right speaker was silent throughout.
``backend.input_mix()`` existed, block 33 had been decoded the week before,
and hard safety rule 5 says in as many words: *verify routing by measurement,
not by intent.* Nothing looked. This is the code path that looks.

It reports rather than refuses, with one exception: an output under test whose
mixer receives nothing cannot be measured, and saying so early is the whole
point.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

#: A mixer level at or below this contributes nothing worth measuring.
SILENT_LEVEL = 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--address", required=True)
    ap.add_argument("--port")
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--link-id", type=int, default=4)
    ap.add_argument("--journal")
    ap.add_argument("--outputs", type=int, nargs="+", default=[1, 2],
                    help="1-based outputs about to be measured or tuned")
    ap.add_argument("--stimulus-input", type=int, default=None,
                    help="1-based DSP input the stimulus arrives on, if known")
    args = ap.parse_args()

    import tune_run

    under_test = [n - 1 for n in args.outputs]
    problems: list[str] = []

    session, backend, transport = tune_run._live_backend(args)
    with session:
        identity = session.handshake()
        running = (
            identity.preset_names[identity.current_preset - 1]
            if identity.current_preset
            else "working area (no preset)"
        )
        print(f"DSP        {transport}")
        print(f"firmware   {identity.firmware}")
        print(f"running    {running}")

        print(f"\n{'out':>6}{'IN1':>6}{'IN2':>6}{'IN3':>6}{'IN4':>6}"
              f"{'gain':>9}{'delay':>8}{'crossover':>18}{'EQ':>4}  ")
        rows = {}
        for out in range(8):
            mix = backend.input_mix(out)
            cfg = backend.read_channel(out)
            rows[out] = (mix, cfg)
            xover = (
                f"{cfg.crossover.high_pass_hz:.0f}-"
                f"{cfg.crossover.low_pass_hz:.0f} @{cfg.crossover.slope_db_oct}"
            )
            mark = " <-- under test" if out in under_test else ""
            mute = " MUTED" if cfg.muted else ""
            print(
                f"  OUT{out + 1}"
                + "".join(f"{v:6d}" for v in mix.levels[:4])
                + f"{cfg.gain_dbfs:>+9.2f}{cfg.delay_samples:>8}"
                f"{xover:>18}{len(cfg.peq):>4}{mute}{mark}"
            )

        print("\nrouting")
        for out in under_test:
            mix, cfg = rows[out]
            fed = [i + 1 for i, v in enumerate(mix.levels[:4]) if v > SILENT_LEVEL]
            if not fed:
                problems.append(
                    f"OUT{out + 1} receives nothing: every input level is zero. "
                    f"A sweep into it measures silence, and deconvolving "
                    f"silence yields a smooth, plausible, entirely wrong curve."
                )
                continue
            print(f"  OUT{out + 1} <- inputs {fed}")
            if args.stimulus_input is not None and args.stimulus_input not in fed:
                problems.append(
                    f"OUT{out + 1} does not take input {args.stimulus_input}, "
                    f"which is where the stimulus arrives. It takes {fed}. "
                    f"This output will be silent during the sweep."
                )
            if cfg.muted:
                problems.append(f"OUT{out + 1} is muted.")

        # Two outputs under test fed by the *same* input are not two
        # measurements. It is the same signal twice, and any difference
        # between them is the loudspeakers and the room, not the channels.
        if args.stimulus_input is not None and len(under_test) > 1:
            shared = [
                out + 1 for out in under_test
                if rows[out][0].levels[args.stimulus_input - 1] > SILENT_LEVEL
            ]
            if len(shared) > 1:
                print(
                    f"\n  note: outputs {shared} all take input "
                    f"{args.stimulus_input}, so a sweep drives them together. "
                    f"That is one acoustic source to measure, not several -- "
                    f"mute the others to measure one at a time."
                )

    print("\nREW")
    try:
        from tuner.measure.rewapi import RewApi

        api = RewApi()
        print(f"  version        {api.version()}")
        print(f"  output device  {api.output_device()!r}")
        held = api.measurements()
        print(f"  measurements   {len(held)} held")
    except Exception as exc:  # noqa: BLE001 -- a pre-flight reports, it does not die
        print(f"  unavailable: {type(exc).__name__}: {exc}")

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nnothing refused. The checks this cannot make are physical: what is "
          "\nactually cabled to each output, and where the stimulus really "
          "enters.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
