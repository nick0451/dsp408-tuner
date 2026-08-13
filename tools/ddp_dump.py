"""Dump or diff vendor ``.DDP`` tune backups.

The bench loop this exists for::

    python tools/ddp_dump.py before.DDP                  # see what is in the box
    # ... change exactly one control in the vendor app, save after.DDP ...
    python tools/ddp_dump.py before.DDP after.DDP        # read off the encoding

Nothing here writes to a device. It reads files the vendor app produced.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tuner.dsp.ddp import diff as ddp_diff  # noqa: E402
from tuner.dsp.ddp import parse  # noqa: E402
from tuner.dsp.protocol import (  # noqa: E402
    EQ_FREQ_TABLE_HZ,
    XOVER_FREQ_TABLE_HZ,
)


def _flag_off_table(freq_hz: int, table: tuple[int, ...]) -> str:
    """Mark frequencies the assumed quantization tables do not contain."""
    return "" if freq_hz in table else "  <- not in table"


def dump(path: Path, show_eq: bool) -> None:
    backup = parse(path.read_bytes())
    print(f"{path.name}: preset {backup.preset_name!r}")
    print()
    print(
        f"{'ch':>3} {'gain_raw':>9} {'delay':>7} {'pol':>4} {'spk':>4} "
        f"{'highpass':>18} {'lowpass':>18}  mix"
    )
    for out in backup.outputs:
        hp = f"{out.h_freq_hz} Hz f{out.h_filter}/l{out.h_level}"
        lp = f"{out.l_freq_hz} Hz f{out.l_filter}/l{out.l_level}"
        mix = ",".join(str(m) for m in out.mix if m) or "-"
        print(
            f"{out.index:>3} {out.gain_raw:>9} {out.delay_samples:>7} "
            f"{out.polar:>4} {out.spk_type:>4} {hp:>18} {lp:>18}  {mix}"
        )

    off_table = [
        (out.index, side, freq)
        for out in backup.outputs
        for side, freq in (("hp", out.h_freq_hz), ("lp", out.l_freq_hz))
        if freq not in XOVER_FREQ_TABLE_HZ
    ]
    if off_table:
        print()
        print("crossover corners absent from XOVER_FREQ_TABLE_HZ:")
        for index, side, freq in off_table:
            print(f"  OUT{index} {side} {freq} Hz")

    if not show_eq:
        print()
        print("(pass --eq for per-band PEQ detail)")
        return

    for out in backup.outputs:
        active = [
            (i, b)
            for i, b in enumerate(out.eq)
            if b.level_raw != 600 or b.freq_hz not in EQ_FREQ_TABLE_HZ
        ]
        if not active:
            continue
        print()
        print(f"OUT{out.index} PEQ bands that differ from the default layout:")
        for i, band in active:
            note = _flag_off_table(band.freq_hz, EQ_FREQ_TABLE_HZ)
            print(
                f"  band {i:>2}  {band.freq_hz:>6} Hz  level_raw {band.level_raw:>4}"
                f"  bw_raw {band.bw_raw:>3}  type {band.type}{note}"
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("before", type=Path)
    ap.add_argument("after", type=Path, nargs="?")
    ap.add_argument("--eq", action="store_true", help="show per-band PEQ detail")
    args = ap.parse_args()

    if args.after is None:
        dump(args.before, args.eq)
        return 0

    changes = ddp_diff(parse(args.before.read_bytes()), parse(args.after.read_bytes()))
    if not changes:
        print("no differences")
        return 0
    print(f"{len(changes)} change(s): {args.before.name} -> {args.after.name}")
    for line in changes:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
