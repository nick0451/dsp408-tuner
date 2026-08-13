"""Measure a DSP-408 crossover corner and fit it against Linkwitz-Riley theory.

Built to answer one question: **does the device honour a crossover frequency
that is not in the tables the APK implied, or does it snap to the nearest
entry?** Those two predict corners several percent apart, against a method that
resolved 500 Hz to 493.1 Hz last session.

Magnitude only. There is no loopback on the Solo's two inputs, so no timing
reference exists and no delay or phase figure may be reported -- see the
timing-reference rule in CLAUDE.md. The corner frequency is a magnitude
feature, so this costs nothing here.

Typical use, with nothing but the Scarlett's line input on the DSP output::

    python tools/bench_crossover.py --lp 450 --fit 150 2000 --electrical-only
    python tools/bench_crossover.py --hp 1234 --fit 300 8000 --electrical-only

Before running:
  * unplug USB-B from the DSP but **leave its power connected** -- the USB
    ground path raises the noise floor by 43 dB, and removing power would
    revert the device to its stored tune;
  * pause host audio;
  * confirm both Scarlett knobs are on their end stops.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares

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

#: Host-API-qualified names. Never indices: MME renumbers when the Windows
#: default output changes, which once pointed a sweep at the PC speakers while
#: still capturing the right input.
#: **These still name MME. `bench_golden.py` moved to WASAPI on 2026-08-09**,
#: after measuring MME's run-to-run scatter as 2-5x worse from 250 Hz to 10 kHz
#: on an unchanged path -- 1.772 dB against 0.384 at 5-7 kHz, at a comfortable
#: -33 dBFS, so not a signal-to-noise story.
#:
#: They are left on MME deliberately rather than switched blind: a host API is
#: part of the measurement chain, and this project's rule is that changing the
#: chain owes a known-answer check. Move each one the next time it runs against
#: hardware, and re-measure something with a known answer as you do -- for this
#: tool, a corner or a band already on record.
OUTPUT_DEVICE = "Speakers (Scarlett Solo USB), MME"
INPUT_DEVICE = "Microphone (Scarlett Solo USB), MME"

SAMPLE_RATE_HZ = 44_100

#: Linkwitz-Riley 4th order sits here at the corner, not -3 dB. Two cascaded
#: 2nd-order Butterworth sections, so the magnitude is x^4/(1+x^4).
LR4_CORNER_DB = -6.02


def lr4_db(
    freqs_hz: np.ndarray, hp_hz: float | None, lp_hz: float | None
) -> np.ndarray:
    """Ideal LR4 magnitude in dB for a high-pass, low-pass or band-pass."""
    mag = np.ones_like(freqs_hz, dtype=np.float64)
    if hp_hz is not None:
        x4 = (freqs_hz / hp_hz) ** 4
        mag *= x4 / (1.0 + x4)
    if lp_hz is not None:
        mag *= 1.0 / (1.0 + (freqs_hz / lp_hz) ** 4)
    return 20.0 * np.log10(mag + 1e-30)


def fit_corners(
    freqs_hz: np.ndarray,
    measured_db: np.ndarray,
    hp_guess: float | None,
    lp_guess: float | None,
) -> tuple[float | None, float | None, float, float]:
    """Least-squares fit of LR4 corner(s) plus a passband offset.

    Returns ``(hp_hz, lp_hz, offset_db, rms_residual_db)``. The offset absorbs
    channel gain, which is not what is being measured here.
    """
    guesses: list[float] = []
    if hp_guess is not None:
        guesses.append(np.log(hp_guess))
    if lp_guess is not None:
        guesses.append(np.log(lp_guess))
    guesses.append(float(np.median(measured_db)))

    def residual(params: np.ndarray) -> np.ndarray:
        i = 0
        hp = float(np.exp(params[i])) if hp_guess is not None else None
        i += hp_guess is not None
        lp = float(np.exp(params[i])) if lp_guess is not None else None
        i += lp_guess is not None
        return lr4_db(freqs_hz, hp, lp) + params[i] - measured_db

    sol = least_squares(residual, np.array(guesses), method="lm")
    i = 0
    hp = float(np.exp(sol.x[i])) if hp_guess is not None else None
    i += hp_guess is not None
    lp = float(np.exp(sol.x[i])) if lp_guess is not None else None
    i += lp_guess is not None
    rms = float(np.sqrt(np.mean(residual(sol.x) ** 2)))
    return hp, lp, float(sol.x[i]), rms


def verdict(label: str, fitted_hz: float, requested_hz: float) -> None:
    """Report the fit against 'honoured' and 'snapped to the table'."""
    from tuner.dsp.protocol import XOVER_FREQ_TABLE_HZ, nearest_xover_index

    snapped = XOVER_FREQ_TABLE_HZ[nearest_xover_index(requested_hz)]
    err_honoured = 100.0 * (fitted_hz - requested_hz) / requested_hz
    err_snapped = 100.0 * (fitted_hz - snapped) / snapped

    print(f"\n  {label}")
    print(f"    set in the app        {requested_hz:8.1f} Hz")
    print(f"    measured corner       {fitted_hz:8.1f} Hz")
    print(f"    if honoured           {err_honoured:+8.2f} %")
    print(f"    if snapped to {snapped:5d}   {err_snapped:+8.2f} %")

    if abs(err_honoured) > 15.0 and abs(err_snapped) > 15.0:
        print(
            "    -> UNCONSTRAINED. Both candidates are far off, which means "
            "the fit band does not bracket this corner rather than that the "
            "device did something exotic. Widen --fit past the corner or drop "
            "this leg. Do not report this number."
        )
    elif abs(err_honoured) < abs(err_snapped) / 2:
        print("    -> HONOURED. Frequency is continuous; fit it continuously.")
    elif abs(err_snapped) < abs(err_honoured) / 2:
        print(f"    -> SNAPPED to {snapped} Hz. The table is a real constraint.")
    else:
        print("    -> AMBIGUOUS. Probe a frequency further from a table entry.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hp", type=float, help="high-pass corner set in the app, Hz")
    ap.add_argument("--lp", type=float, help="low-pass corner set in the app, Hz")
    ap.add_argument(
        "--fit",
        type=float,
        nargs=2,
        metavar=("LOW_HZ", "HIGH_HZ"),
        required=True,
        help="frequency range to fit over, stated explicitly rather than guessed",
    )
    ap.add_argument("--level-dbfs", type=float, default=-6.0)
    ap.add_argument(
        "--electrical-only",
        action="store_true",
        help="assert no transducer is connected to this output, permitting a "
        "stimulus above the default tweeter-safe ceiling. Rule 4 in "
        "CLAUDE.md: raising a ceiling is a deliberate act.",
    )
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--points", type=int, default=400)
    ap.add_argument("--temperature-c", type=float, default=None)
    ap.add_argument("--skip-linearity", action="store_true")
    ap.add_argument(
        "--linearity-freqs",
        type=float,
        nargs="+",
        default=None,
        metavar="HZ",
        help="tones for the linearity check. The defaults assume a full-range "
        "path; through a crossover-filtered channel the out-of-band tones "
        "land in the noise floor and the check false-positives. Give "
        "frequencies inside this channel's passband.",
    )
    ap.add_argument("--note", action="append", default=[], metavar="KEY=VALUE")
    args = ap.parse_args()

    if args.hp is None and args.lp is None:
        ap.error("give at least one of --hp / --lp")

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

    device = (INPUT_DEVICE, OUTPUT_DEVICE)

    if not args.skip_linearity:
        print("Level linearity (~14 s) ...")
        kwargs = {}
        if args.linearity_freqs:
            kwargs["freqs_hz"] = tuple(args.linearity_freqs)
        result = measure_level_linearity(
            sample_rate_hz=SAMPLE_RATE_HZ,
            output_channel=0,
            input_channel=1,
            device=device,
            limit=limit,
            **kwargs,
        )
        require_linear_path(result)
        print(f"  gain spread {result.spread_db:.2f} dB across level -- linear.")

    notes = dict(n.split("=", 1) for n in args.note)
    notes.setdefault("purpose", "crossover corner vs frequency-table quantization")

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

    print(f"Sweep, median of {args.repeats} ...")
    measurements = capture_sweep(config, session)
    measurement = measurements[1]

    lo, hi = args.fit
    freqs = log_freqs(lo, hi, args.points)
    measured_db = measurement.magnitude_dbfs(freqs)

    hp, lp, offset, rms = fit_corners(freqs, measured_db, args.hp, args.lp)

    print(f"\nFit over {lo:.0f}-{hi:.0f} Hz, {args.points} log-spaced points")
    print(f"  passband offset  {offset:+.2f} dB")
    print(f"  rms residual     {rms:.3f} dB")
    if rms > 1.0:
        print("  WARNING: residual is large; the model may not match the filter.")

    if hp is not None:
        verdict("HIGH-PASS", hp, args.hp)
    if lp is not None:
        verdict("LOW-PASS", lp, args.lp)

    prov = measurement.provenance
    print(
        f"\n  provenance: {prov.device} @ {prov.sample_rate_hz} Hz, "
        f"{prov.timestamp:%Y-%m-%d %H:%M}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
