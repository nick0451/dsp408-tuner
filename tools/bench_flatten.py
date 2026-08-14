"""Flatten outputs to a clean baseline for a bench tune, and put them back.

Two commands, and the second is the reason the first is safe to run::

    python tools/bench_flatten.py flatten --address <MAC> --outputs 1 2 \
        --snapshot-out snapshots/pre-bench.json --confirm
    python tools/bench_flatten.py restore --address <MAC> \
        --snapshot snapshots/pre-bench.json

**This is the car's DSP.** Flattening destroys whatever tune those outputs
carry, and the device has no undo -- every parameter write goes straight to
non-volatile storage. The snapshot is therefore captured, saved and verified
*before* anything is written, and ``restore`` puts every block back and
re-reads to prove it.

What "flatten" does, and does not:

* **EQ: every band set flat.** All :data:`ADDRESSABLE_BANDS`, not just the
  loaded ones, so nothing survives in a slot the fit will not overwrite.
* **Crossover: corners opened**, keeping the alignment and slope the channel
  already has. ``ChannelConfig`` cannot express Defeat -- the filter type byte
  is carried through unchanged by design -- so "open" means corners far enough
  out to be transparent in band, not a bypass.
* **Gain and delay: untouched.** Both are real settings the operator chose,
  neither shapes magnitude in a way a PEQ fit is confused by, and leaving them
  narrows what has to be restored.

The crossover is the part that matters for a 2.1 bench system: with the DSP
high-passing at 450 Hz the subwoofer receives nothing at all, and the
measurement is of a satellite pretending to be a loudspeaker.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tuner.dsp.backend import Crossover  # noqa: E402
from tuner.dsp.dsp408_spp import ADDRESSABLE_BANDS  # noqa: E402

#: Corners that pass the audible band. Not a bypass -- see the module
#: docstring. A Linkwitz-Riley 12 dB/octave high-pass at 20 Hz is within a
#: decibel by 40 Hz, and the matching low-pass at 20 kHz is transparent
#: everywhere this project measures.
OPEN_HIGH_PASS_HZ = 20.0
OPEN_LOW_PASS_HZ = 20_000.0

def _describe(label: str, config) -> None:
    print(
        f"  {label}: gain {config.gain_dbfs:+.2f} dB, delay "
        f"{config.delay_samples} samples, crossover "
        f"{config.crossover.high_pass_hz:.0f}-{config.crossover.low_pass_hz:.0f} Hz "
        f"@ {config.crossover.slope_db_oct} dB/oct, "
        f"{len(config.peq)} non-flat EQ band(s)"
    )
    for i, band in enumerate(config.peq):
        print(
            f"      {i}: {band.freq_hz:8.0f} Hz  {band.gain_dbfs:+6.2f} dB  "
            f"Q {band.q:5.3f}"
        )


def cmd_flatten(args) -> int:
    import tune_run

    from tuner.dsp import snapshot as snap

    # **Refuse to overwrite a restore point, before contacting the device.**
    #
    # Learned the expensive way on 2026-08-13. This tool captures before it
    # writes, which is right, and saved to a fixed path, which was not: three
    # runs against the same path -- a dry one, one stopped part-way by the
    # blast-radius cap with OUT1 already written, and one that completed --
    # left a file holding OUT1 *already flat*. The pristine state was gone.
    #
    # The failure mode is the nasty kind: the replacement snapshot looks
    # healthy, carries a valid digest, and restores the device to a state
    # nobody asked for. A restore point a re-run can silently replace is not
    # a restore point.
    out_path = Path(args.snapshot_out)
    if out_path.exists():
        raise SystemExit(
            f"{out_path} already exists, and overwriting it would destroy a "
            f"restore point.\n\n"
            f"If the earlier run wrote nothing, that file is the pristine "
            f"state and is the one you want -- keep it. If it wrote part of "
            f"the way, that file is still closer to pristine than anything "
            f"captured now. Either way, write this one somewhere new."
        )

    outputs = [n - 1 for n in args.outputs]
    session, backend, transport = tune_run._live_backend(args, writable=args.confirm)
    with session:
        identity = session.handshake()
        # Name the running preset, because that is the operator's own manual
        # fallback and the thing they will reach for if tonight goes wrong.
        running = (
            identity.preset_names[identity.current_preset - 1]
            if identity.current_preset
            else "working area (no preset)"
        )
        print(
            f"DSP {transport}   firmware {identity.firmware}   "
            f"running: {running}"
        )

        before = {out: backend.read_channel(out) for out in outputs}
        print("\ncurrent state:")
        for out in outputs:
            _describe(f"OUT{out + 1}", before[out])

        shot = snap.capture(
            backend.device, identity, transport_name=transport,
            notes={"stage": "bench-flatten", "outputs": str(args.outputs)},
        )
        evidence = shot.save(Path(args.snapshot_out))
        print(f"\nrestore point {args.snapshot_out}  ({evidence.digest[:16]})")
        print(
            f"  put it back with:\n"
            f"    python tools/bench_flatten.py restore --address {args.address} "
            f"--snapshot {args.snapshot_out}"
        )

        # An **empty** chain, not 31 explicit flat bands. Under
        # `PeqPolicy.EXCLUSIVE` the planner flattens every slot from
        # `len(config.peq)` to `ADDRESSABLE_BANDS`, so an empty request
        # clears all 31 -- while asking for 31 explicitly trips the
        # `max_peq_per_channel` refusal, which exists to stop a *fit*
        # spending bands the firmware will not execute. Requesting nothing
        # and letting the policy clear the rest is the mechanism the backend
        # already has, rather than an argument with it.
        target = {
            out: replace(
                before[out],
                peq=(),
                crossover=Crossover(
                    high_pass_hz=OPEN_HIGH_PASS_HZ,
                    low_pass_hz=OPEN_LOW_PASS_HZ,
                    slope_db_oct=before[out].crossover.slope_db_oct,
                ),
            )
            for out in outputs
        }

        print("\nwould write:")
        for out in outputs:
            print(
                f"  OUT{out + 1}: EQ cleared on all {ADDRESSABLE_BANDS} slots, "
                f"crossover {OPEN_HIGH_PASS_HZ:.0f}-{OPEN_LOW_PASS_HZ:.0f} Hz, "
                f"gain and delay unchanged"
            )
        if not args.confirm:
            print("\nNothing written. Add --confirm to apply.")
            return 0

        backend.device.arm_writes("bench flatten for a known baseline", evidence)
        for out in outputs:
            backend.write_channel(out, target[out])
        print("\nwritten. reading back:")
        for out in outputs:
            _describe(f"OUT{out + 1}", backend.read_channel(out))
    return 0


def cmd_restore(args) -> int:
    import tune_run

    from tuner.dsp import snapshot as snap

    session, backend, transport = tune_run._live_backend(args, writable=True)
    with session:
        identity = session.handshake()
        shot = snap.load(Path(args.snapshot))
        print(f"DSP {transport}\nrestoring {args.snapshot}")
        residual = snap.restore(
            backend.device, shot, dry_run=False, reason="bench flatten rollback"
        )
        print(f"device vs snapshot after restore: {residual or 'identical'}")
        for out in range(8):
            cfg = backend.read_channel(out)
            print(
                f"  OUT{out + 1}: gain {cfg.gain_dbfs:+.2f} dB, "
                f"{cfg.crossover.high_pass_hz:.0f}-"
                f"{cfg.crossover.low_pass_hz:.0f} Hz, {len(cfg.peq)} band(s)"
            )
        _ = identity
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("flatten", help="flat EQ and open crossovers, after a snapshot")
    p.add_argument("--address", required=True)
    p.add_argument("--port")
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--link-id", type=int, default=4)
    p.add_argument("--journal")
    p.add_argument("--outputs", type=int, nargs="+", required=True,
                   help="1-based output numbers")
    p.add_argument("--snapshot-out", required=True)
    p.add_argument("--confirm", action="store_true", help="actually write")
    # The blast-radius cap defaults to one channel per session, which is right
    # for a bring-up rung and wrong here: flattening a stereo pair is two
    # channels by definition. Raised deliberately and only as far as the
    # request needs, rather than left to be discovered mid-write with one
    # channel already changed -- which is exactly how it was discovered.
    p.set_defaults(func=cmd_flatten, max_writes=400, max_channels=8)

    p = sub.add_parser("restore", help="put a snapshot back and verify")
    p.add_argument("--address", required=True)
    p.add_argument("--port")
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--link-id", type=int, default=4)
    p.add_argument("--journal")
    p.add_argument("--snapshot", required=True)
    p.set_defaults(func=cmd_restore, max_writes=4000, max_channels=8)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
