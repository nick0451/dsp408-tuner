"""The gate that must pass before a stimulus reaches the car. Refuses, loudly.

    python tools/car_preflight.py --address <MAC> --outputs 3 4 5 6 \
        --input-gain 50 --require-flat

**Run this every time, before anything else.** The drivers on this car have no
passive crossovers, so a wrong corner is a dead tweeter rather than a bad
measurement, and nothing downstream can see it: the write succeeds, the
readback matches, the fit is good, and the driver is gone.

What it checks, all against a live read:

* **Crossover corners and slope** per output, from
  :mod:`tuner.orchestrate.carlimits`, which encodes ``docs/car-system.md``.
* **Channel gain** against the operator's own clip point (54 on the iOS 0-60
  scale, which is -6.0 dB).
* **Master volume** against 54. Global -- one write moves every output.
* **Input gain** against 90. Unreadable over our protocol, so it must be
  declared and is then checked; omitting it is a refusal, not a default.
* **EQ flat**, with ``--require-flat``, because a baseline measured through a
  loaded EQ is not a baseline.

It does **not** verify routing. Which output drives which driver is the
operator's declaration and is recorded as ``ChannelSpec.basis``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tuner.orchestrate.carlimits import (  # noqa: E402
    SPEC_BY_OUTPUT,
    Violation,
    check_channel,
    check_master_volume,
    require_declared_input_gain,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--address")
    ap.add_argument("--port")
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--link-id", type=int, default=4)
    ap.add_argument("--journal")
    ap.add_argument("--outputs", type=int, nargs="+", required=True,
                    help="1-based outputs about to be measured or tuned")
    ap.add_argument("--input-gain", type=int, default=None,
                    help="what the iOS app shows for input gain, 0-100. "
                         "Unreadable over our protocol; required.")
    ap.add_argument("--require-flat", action="store_true",
                    help="also require every EQ band flat, for a baseline")
    args = ap.parse_args()

    import tune_run

    problems: list[Violation] = []
    session, backend, transport = tune_run._live_backend(args)
    with session:
        identity = session.handshake()
        print(f"DSP        {transport}")
        print(f"firmware   {identity.firmware}")
        mv = identity.system_blocks.get(5)
        declared = (
            args.input_gain if args.input_gain is not None else "NOT DECLARED"
        )
        print(f"master     {mv[0] if mv else '?'}   (limit 54)")
        print(f"input gain {declared}   (limit 90, unreadable by us)\n")

        problems += check_master_volume(identity.system_blocks)
        problems += require_declared_input_gain(args.input_gain)

        print(f"{'out':>5}  {'driver':34}  {'crossover':>18}  {'gain':>8}  {'EQ':>3}")
        for one_based in args.outputs:
            out = one_based - 1
            spec = SPEC_BY_OUTPUT.get(out)
            cfg = backend.read_channel(out)
            xo = cfg.crossover
            hp = f"{xo.high_pass_hz:.0f}" if xo.high_pass_hz else "off"
            lp = f"{xo.low_pass_hz:.0f}" if xo.low_pass_hz else "off"
            loaded = sum(1 for b in cfg.peq if abs(b.gain_dbfs) > 0.05)
            print(f"{spec.label if spec else f'OUT{one_based}':>5}  "
                  f"{(spec.driver if spec else '?'):34}  "
                  f"{hp:>7}-{lp:<7}@{xo.slope_db_oct:<2}  "
                  f"{cfg.gain_dbfs:+7.1f}  {loaded:3d}")
            if spec is None:
                problems.append(Violation(
                    f"OUT{one_based}", "no spec in carlimits.CAR_DSP408", True
                ))
                continue
            problems += check_channel(spec, cfg, require_flat=args.require_flat)

    fatal = [p for p in problems if p.fatal]
    warn = [p for p in problems if not p.fatal]

    if warn:
        print("\nwarnings:")
        for p in warn:
            print(f"  [warn] {p.where}: {p.message}")
    if fatal:
        print(f"\n{len(fatal)} BLOCKING problem(s):")
        for p in fatal:
            print(f"  [STOP] {p.where}: {p.message}")
        print("\nDo not emit a stimulus until these are fixed.")
        return 1
    print("\nall checks passed. Safe to measure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
