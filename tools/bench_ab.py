"""Switch the bench pair between flat and tuned, level-matched, for listening.

    python tools/bench_ab.py flat   --address <MAC>
    python tools/bench_ab.py tuned  --address <MAC>

**Level-matched, because otherwise the test measures loudness.** Every filter
in this tune is a cut, so the tuned setting is **2.23 dB quieter** where music
has its energy (200 Hz - 5 kHz, measured). Two decibels is comfortably enough
to make a system sound "better" regardless of its tonality, and it is the
single most reliable way to fool yourself in an A/B. So ``tuned`` raises the
channel gain by the measured offset and ``flat`` puts it back.

The offset is weighted over 200 Hz - 5 kHz rather than the full sweep,
because a level averaged from 20 Hz to 20 kHz is not the level anyone hears.

**Both channels get the same filters, and only the left was measured.** REW
swept channel L, which the mixer routes to OUT1; OUT2 never saw a stimulus.
Copying the fit across assumes a matched pair at a roughly symmetric seat.
That assumption is worth a lot less than a measurement and a great deal more
than listening to a half-tuned system, which is what the alternative was.
Measure the right channel properly before believing anything about imaging.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tuner.dsp.backend import Biquad, FilterType  # noqa: E402

OUTPUTS = (0, 1)

#: What REW's matcher produced against a five-position spatial average at the
#: listening position, 2026-08-13, with a Full-range target plus room curve
#: and boost capped at 3 dB per filter and 0 dB overall.
BANDS = tuple(
    Biquad(freq_hz=f, gain_dbfs=g, q=q, kind=FilterType.PEAKING)
    for f, g, q in (
        (205.0, -0.50, 9.960),
        (227.0, -1.10, 9.770),
        (318.0, -3.20, 4.940),
        (411.0, -1.10, 3.820),
        (476.0, -5.20, 4.990),
        (827.0, -2.30, 3.790),
        (2782.0, -2.90, 5.290),
    )
)

#: The gain these channels carry with no tune, and the compensation. Measured,
#: not estimated: the difference in power-averaged level over 200 Hz - 5 kHz
#: between the before and after spatial averages.
FLAT_GAIN_DBFS = -10.0
LEVEL_OFFSET_DB = 2.2


#: Working file, deliberately overwritten every toggle. **Not a restore
#: point** -- those are ``pre-bench-*`` and ``pre-rew-tune-*``, and
#: ``bench_flatten`` refuses to overwrite them for good reason. This one
#: exists only because ``arm_writes`` requires a snapshot taken this session
#: from this device, and an A/B that captured a new precious file every few
#: seconds would bury the two that matter.
AB_SNAPSHOT = Path("snapshots/ab-session.json")


def apply(args, bands, gain_dbfs: float, label: str) -> int:
    import tune_run

    from tuner.dsp import snapshot as snap

    session, backend, transport = tune_run._live_backend(args, writable=True)
    with session:
        identity = session.handshake()
        shot = snap.capture(
            backend.device, identity, transport_name=transport,
            notes={"stage": "bench A/B", "setting": label.split()[0]},
        )
        AB_SNAPSHOT.parent.mkdir(exist_ok=True)
        backend.device.arm_writes(
            f"bench A/B: {label.split()[0]}", shot.save(AB_SNAPSHOT)
        )
        for out in OUTPUTS:
            live = backend.read_channel(out)
            backend.write_channel(
                out, replace(live, peq=bands, gain_dbfs=gain_dbfs)
            )
        print(f"{label}  ({transport})")
        for out in OUTPUTS:
            back = backend.read_channel(out)
            print(
                f"  OUT{out + 1}: gain {back.gain_dbfs:+.2f} dB, "
                f"{len(back.peq)} EQ band(s)"
            )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("flat", "tuned"):
        p = sub.add_parser(name)
        p.add_argument("--address", required=True)
        p.add_argument("--port")
        p.add_argument("--channel", type=int, default=1)
        p.add_argument("--link-id", type=int, default=4)
        p.add_argument("--journal")
        p.set_defaults(which=name, max_writes=400, max_channels=8)
    args = ap.parse_args()

    if args.which == "flat":
        return apply(args, (), FLAT_GAIN_DBFS, "FLAT   (no EQ, reference level)")
    return apply(
        args,
        BANDS,
        FLAT_GAIN_DBFS + LEVEL_OFFSET_DB,
        f"TUNED  ({len(BANDS)} bands, +{LEVEL_OFFSET_DB:.1f} dB to match level)",
    )


if __name__ == "__main__":
    raise SystemExit(main())
