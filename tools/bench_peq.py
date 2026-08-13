"""Measure one DSP-408 PEQ band and test it against the RBJ model.

Built to answer the question the optimizer rests on and nobody has ever asked
the hardware: **when the device is set to a bandwidth, what filter does it
actually run?**

``tuner.optimize.biquad`` fits RBJ peaking biquads, and
``protocol.q_from_bw_raw`` converts the device's stored bandwidth to Q by the
standard relation ``Q = sqrt(2^N)/(2^N - 1)``. That relation is verified against
the vendor app's *display* on five points. It has never been checked against
sound. If the firmware's bandwidth convention differs from RBJ's, every fit the
optimizer produces is systematically wrong in a way that looks like a mediocre
optimizer rather than a wrong model -- the same shape of error as the
frequency-table assumption this project already had to unwind.

Two questions, answered separately, because they fail differently:

1. **Is the shape right?** Fit an RBJ peaking section with everything free. A
   large residual means the device is not running an RBJ peaking biquad at all,
   and no amount of remapping Q will fix it.
2. **Is the Q mapping right?** With the shape confirmed, compare the fitted
   filter's actual bandwidth -- measured numerically at both candidate
   conventions -- against the ``N`` octaves the device was told to use.

.. warning::
   **Do not run this at +6 dB.** A peaking filter's half-gain points sit at
   ``G/2`` and its -3 dB points at ``G-3``; those are equal when ``G = 6``. At
   +6 dB the two candidate conventions predict the *same curve* and the
   experiment is null. Use +12 dB, where they are 3 dB apart. This is the
   "pick the probe point that maximises hypothesis separation" rule from
   STATE.md, applied before running rather than after.

Magnitude only -- there is no loopback on the Solo, so no phase or delay figure
may be reported. Filter shape is a magnitude feature, so that costs nothing.

Typical use, with only the Scarlett's line input on the DSP output::

    python tools/bench_peq.py --freq 1000 --bw-raw 25  --gain-db 12 --electrical-only
    python tools/bench_peq.py --freq 1000 --bw-raw 106 --gain-db 12 --electrical-only
    python tools/bench_peq.py --freq 1000 --bw-raw 195 --gain-db 12 --electrical-only

Set the band in the vendor app, **save a .DDP**, then run. Read ``bw_raw`` out
of the saved file with ``tools/ddp_dump.py`` rather than trusting the app's
rounded Q display -- feeding a displayed Q back through ``bw_raw_for_q`` is
exactly the mistake that produced two wrong predictions earlier in this project.

Before running:
  * unplug USB-B from the DSP but **leave its power connected**;
  * pause host audio;
  * confirm both Scarlett knobs are on their end stops;
  * leave every other band flat and the crossovers wide.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import brentq, least_squares

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tuner.dsp.backend import Biquad, FilterType  # noqa: E402
from tuner.dsp.protocol import (  # noqa: E402
    bandwidth_octaves,
    gain_dbfs,
    q_from_bw_raw,
)
from tuner.measure import (  # noqa: E402
    CaptureConfig,
    SessionInfo,
    capture_sweep,
    log_freqs,
    measure_level_linearity,
    require_linear_path,
)
from tuner.optimize.biquad import response_db  # noqa: E402
from tuner.safety import ChannelLimit  # noqa: E402

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

#: Gain at which the half-gain and -3 dB bandwidth conventions coincide.
DEGENERATE_GAIN_DB = 6.0

#: ``bw_raw`` values seen across every saved tune in the repository: 0.29 to
#: 1.39 octaves, Q 4.97 down to 1.00. **Observed, not a proven device limit** --
#: nobody has probed the endpoints. Used only to judge whether a fitted Q is
#: something the device could plausibly have been running.
OBSERVED_BW_RAW = (24, 134)


def _plausible_q_range() -> tuple[float, float]:
    lo = q_from_bw_raw(OBSERVED_BW_RAW[1])
    hi = q_from_bw_raw(OBSERVED_BW_RAW[0])
    return lo / 4.0, hi * 4.0


def peaking_db(
    freqs_hz: np.ndarray, f0_hz: float, q: float, gain_db: float
) -> np.ndarray:
    """RBJ peaking magnitude in dB, via the project's own evaluator.

    Deliberately reuses :func:`tuner.optimize.biquad.response_db` rather than
    reimplementing the algebra: the thing under test is the model the optimizer
    actually uses, so a second implementation here could agree with the device
    while the optimizer still disagreed with both.
    """
    band = Biquad(freq_hz=f0_hz, gain_dbfs=gain_db, q=q, kind=FilterType.PEAKING)
    return response_db((band,), freqs_hz, sample_rate_hz=SAMPLE_RATE_HZ)


def fit_peaking(
    freqs_hz: np.ndarray,
    measured_db: np.ndarray,
    f0_guess: float,
    q_guess: float,
    gain_guess_db: float,
) -> tuple[float, float, float, float, float]:
    """Least-squares fit of one peaking section plus a flat offset.

    Returns ``(f0_hz, q, gain_db, offset_db, rms_db)``. The offset absorbs
    channel gain and interface response, neither of which is under test.
    """

    def unpack(p: np.ndarray) -> tuple[float, float, float, float]:
        return float(np.exp(p[0])), float(np.exp(p[1])), float(p[2]), float(p[3])

    def residual(p: np.ndarray) -> np.ndarray:
        f0, q, gain, offset = unpack(p)
        return peaking_db(freqs_hz, f0, q, gain) + offset - measured_db

    guess = np.array(
        [
            np.log(f0_guess),
            np.log(q_guess),
            gain_guess_db,
            float(np.median(measured_db)),
        ]
    )
    sol = least_squares(residual, guess, method="lm", max_nfev=20_000)
    f0, q, gain, offset = unpack(sol.x)
    rms = float(np.sqrt(np.mean(residual(sol.x) ** 2)))
    return f0, q, gain, offset, rms


def bandwidth_octaves_at(f0_hz: float, q: float, gain_db: float, level_db: float):
    """Width in octaves between the two points where the filter hits ``level_db``.

    Solved numerically rather than algebraically so that the answer describes
    the filter the evaluator actually produces -- including bilinear-transform
    frequency warping, which matters at the top of the band and which a
    closed-form analogue expression would quietly omit.

    Returns ``None`` when the level is not crossed on both sides, which happens
    if ``level_db`` is beyond the filter's own peak.
    """
    if gain_db == 0:
        return None
    sign = 1.0 if gain_db > 0 else -1.0

    def excess(f: float) -> float:
        at = float(peaking_db(np.array([f]), f0_hz, q, gain_db)[0])
        return sign * (at - level_db)

    if excess(f0_hz) <= 0:
        return None

    edges = []
    for direction in (0.5, 2.0):
        near, far = f0_hz, f0_hz
        for _ in range(24):
            far *= direction
            if far <= 1.0 or far >= SAMPLE_RATE_HZ / 2.0 - 1.0:
                return None
            if excess(far) < 0:
                break
            near = far
        else:
            return None
        lo, hi = (far, near) if direction < 1 else (near, far)
        edges.append(brentq(excess, lo, hi, xtol=1e-6))

    return float(np.log2(edges[1] / edges[0]))


def report_conventions(
    f0_hz: float, q: float, gain_db: float, requested_octaves: float
) -> None:
    """Which bandwidth definition explains the filter the device ran."""
    candidates = {
        "half-gain points (RBJ, what our code assumes)": gain_db / 2.0,
        "-3 dB from the peak": gain_db - 3.0 * (1.0 if gain_db > 0 else -1.0),
    }

    print(f"\n  Bandwidth convention (device was told N = {requested_octaves:.2f} oct)")
    best, best_err = None, float("inf")
    for name, level in candidates.items():
        octaves = bandwidth_octaves_at(f0_hz, q, gain_db, level)
        if octaves is None:
            print(f"    {name:<46} not crossed")
            continue
        err = octaves - requested_octaves
        print(
            f"    {name:<46} {octaves:5.2f} oct   "
            f"{err:+5.2f} ({100 * err / requested_octaves:+6.1f} %)"
        )
        if abs(err) < best_err:
            best, best_err = name, abs(err)

    if abs(gain_db) <= DEGENERATE_GAIN_DB + 0.5:
        print(
            "\n    -> INCONCLUSIVE BY CONSTRUCTION. At +/-6 dB the two "
            "conventions\n       coincide exactly. Re-run at 12 dB."
        )
        return

    tolerance = 0.05 * requested_octaves
    if best_err <= tolerance:
        print(f"\n    -> matches: {best}")
        if best.startswith("half-gain"):
            print("       Our model is right. q_from_bw_raw and biquad.py stand.")
        else:
            print(
                "       *** OUR MODEL IS WRONG. *** q_from_bw_raw assumes the\n"
                "       half-gain convention. Every optimizer fit is skewed.\n"
                "       Fix protocol.q_from_bw_raw before trusting any tune."
            )
    else:
        print(
            "\n    -> NEITHER convention matches within 5%. Either the device\n"
            "       is not running a textbook peaking section, or the band was\n"
            "       not set to the values passed on the command line. Check the\n"
            "       saved .DDP before concluding anything about the firmware."
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--freq", type=float, required=True, help="band centre set in the app, Hz"
    )
    ap.add_argument(
        "--bw-raw",
        type=int,
        required=True,
        help="raw bandwidth as stored in the .DDP, NOT the app's rounded Q",
    )
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--gain-db", type=float, help="band gain set in the app, dB")
    group.add_argument("--level-raw", type=int, help="raw level from the .DDP")
    ap.add_argument(
        "--fit",
        type=float,
        nargs=2,
        metavar=("LOW_HZ", "HIGH_HZ"),
        default=None,
        help="fit range; defaults to four octaves either side of --freq",
    )
    ap.add_argument("--level-dbfs", type=float, default=-12.0)
    ap.add_argument(
        "--electrical-only",
        action="store_true",
        help="assert no transducer is connected to this output, permitting a "
        "stimulus above the default tweeter-safe ceiling.",
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
        help="tones for the linearity check, inside this channel's passband",
    )
    ap.add_argument("--note", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument(
        "--differential",
        action="store_true",
        help="take a reference sweep with the band FLAT first, then divide. "
        "Cancels the speaker, the room, the microphone and the interface, "
        "leaving only the filter. Strongly preferred, and the only way to "
        "do this without removing the DSP from the car.",
    )
    split = ap.add_mutually_exclusive_group()
    split.add_argument(
        "--reference-out",
        metavar="PATH",
        help="take ONLY the flat reference sweep, write it to PATH and stop. "
        "Splits the differential into two invocations so the band can be "
        "changed between them without a blocking prompt.",
    )
    split.add_argument(
        "--reference-in",
        metavar="PATH",
        help="reuse the reference written by --reference-out and take only the "
        "measurement sweep. Refuses if the two runs disagree about "
        "anything that would make the division meaningless.",
    )
    ap.add_argument(
        "--allow-level-mismatch",
        action="store_true",
        help="permit --reference-in at a different --level-dbfs. Only for the "
        "headroom null test: a level difference is a CONSTANT in dB, so "
        "it lands entirely in the fit's offset term and leaves centre, Q "
        "and gain untouched. The offset then reads out the flat path's "
        "level-linearity for free.",
    )
    args = ap.parse_args()

    if (args.reference_out or args.reference_in) and not args.differential:
        ap.error("--reference-out/--reference-in only apply to --differential")
    if args.allow_level_mismatch and not args.reference_in:
        ap.error("--allow-level-mismatch only applies to --reference-in")

    gain_db = args.gain_db if args.gain_db is not None else gain_dbfs(args.level_raw)
    requested_octaves = bandwidth_octaves(args.bw_raw)
    predicted_q = q_from_bw_raw(args.bw_raw)

    print("Band as set in the app")
    print(f"  centre        {args.freq:9.1f} Hz")
    print(f"  bw_raw        {args.bw_raw:9d}  = {requested_octaves:.2f} octaves")
    print(f"  predicted Q   {predicted_q:9.3f}  (half-gain convention)")
    print(f"  gain          {gain_db:+9.2f} dB")

    if abs(abs(gain_db) - DEGENERATE_GAIN_DB) < 0.5:
        print(
            "\n  WARNING: at +/-6 dB the half-gain and -3 dB conventions are\n"
            "  identical, so this run cannot tell them apart. Use 12 dB."
        )

    if args.level_dbfs + gain_db > -1.0:
        print(
            f"\n  WARNING: stimulus {args.level_dbfs:+.1f} dBFS plus a "
            f"{gain_db:+.1f} dB boost peaks near full scale.\n"
            f"  Lower --level-dbfs or the capture will clip inside the band "
            f"being measured."
        )

    if gain_db > 0 and not args.electrical_only:
        # The boost happens *inside the DSP*, downstream of everything
        # tuner.safety controls. Our limiter sees the stimulus we send; the
        # driver sees that plus the band's gain. Nothing in the safety layer
        # can know about it, so it has to be said here.
        print(
            f"\n  ** The {gain_db:+.1f} dB boost is applied by the DSP, "
            f"DOWNSTREAM of the safety limiter. **\n"
            f"  The limiter caps what we transmit; the driver gets that plus "
            f"{gain_db:.1f} dB.\n"
            f"  Reduce --level-dbfs by at least {gain_db:.0f} dB from whatever "
            f"you would normally use,\n"
            f"  or measure electrically with nothing connected to the output."
        )

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
        print("\nLevel linearity (~14 s) ...")
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
    notes.setdefault("purpose", "PEQ band shape vs the RBJ model")
    notes.setdefault("bw_raw", str(args.bw_raw))
    notes.setdefault("band_freq_hz", str(args.freq))
    notes.setdefault("band_gain_db", f"{gain_db:.2f}")

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

    lo, hi = args.fit or (args.freq / 16.0, min(args.freq * 16.0, 20_000.0))
    freqs = log_freqs(lo, hi, args.points)

    if args.differential:
        print("\n" + "=" * 68)
        print("DIFFERENTIAL MODE -- two sweeps, and the difference is the filter")
        print("=" * 68)
        print(
            "\nEverything common to both sweeps cancels: the speaker, the room,\n"
            "the microphone, the interface, the cable. What is left is the band\n"
            "and nothing else. Keep the microphone still and change nothing but\n"
            "the band between the two sweeps."
        )

        # The reference and the measurement must share the frequency axis they
        # are subtracted on, and the stimulus level: magnitude_dbfs is NOT
        # level-normalised, because deconvolution divides by the *unscaled*
        # sweep. A reference taken 6 dB quieter would put a 6 dB step into the
        # "filter", and the fit's free offset term would swallow it silently.
        signature = {
            "fit_lo_hz": lo,
            "fit_hi_hz": hi,
            "points": float(args.points),
            "level_dbfs": args.level_dbfs,
            "sample_rate_hz": float(SAMPLE_RATE_HZ),
        }

        expected_offset_db = 0.0
        if args.reference_in:
            stored = np.load(args.reference_in)
            for key, want in signature.items():
                if key == "level_dbfs" and args.allow_level_mismatch:
                    # Deliberate, and only useful for the headroom null test.
                    # A stimulus-level difference is a constant in dB, so it
                    # goes entirely into the fit's offset term; centre, Q and
                    # gain come back unchanged. That turns "re-measure the
                    # boosted band 6 dB quieter" from two sweeps plus an
                    # operator round-trip into one sweep and nothing else --
                    # and the offset, which should equal the level difference
                    # exactly, reads out the flat path's level-linearity as a
                    # by-product.
                    expected_offset_db = args.level_dbfs - float(stored[key])
                    continue
                got = float(stored[key])
                if abs(got - want) > 1e-9:
                    print(
                        f"\n  REFUSED: {args.reference_in} was taken with "
                        f"{key} = {got:g}, this run wants {want:g}.\n"
                        f"  Dividing sweeps that disagree about that does not "
                        f"cancel anything."
                    )
                    return 2
            reference_db = stored["reference_db"]
            print(f"\n  Reference loaded from {args.reference_in}")
        else:
            if not args.reference_out:
                input(
                    f"\n  1. Set band at {args.freq:.0f} Hz to 0 dB (flat), "
                    f"leaving its\n     frequency and bandwidth alone. Press "
                    f"Enter when ready: "
                )
            print(f"\n  Reference sweep, median of {args.repeats} ...")
            reference_db = capture_sweep(config, session)[1].magnitude_dbfs(freqs)

        if args.reference_out:
            np.savez(
                args.reference_out,
                freqs=freqs,
                reference_db=reference_db,
                **signature,
            )
            print(
                f"  Reference written to {args.reference_out}\n\n"
                f"  Now set the band to {gain_db:+.1f} dB, bw_raw "
                f"{args.bw_raw}, at {args.freq:.0f} Hz,\n"
                f"  then re-run with --reference-in {args.reference_out}."
            )
            return 0

        if not args.reference_in:
            input(
                f"\n  2. Now set that band to {gain_db:+.1f} dB, bw_raw "
                f"{args.bw_raw}. Press Enter: "
            )
        print(f"  Measurement sweep, median of {args.repeats} ...")
        measurement = capture_sweep(config, session)[1]
        measured_db = measurement.magnitude_dbfs(freqs) - reference_db

        out_of_band = (freqs < args.freq / 8.0) | (freqs > args.freq * 8.0)
        residual = np.abs(measured_db[out_of_band] - expected_offset_db)
        if residual.size:
            # Judge this on the MEDIAN, not the maximum.
            #
            # The failures this check exists to catch -- a mic that moved, a
            # level that shifted, the wrong band edited -- all move the whole
            # out-of-band region together. The median sees them immediately.
            # The maximum does not distinguish them from one noisy bin, and
            # there are always noisy bins: the extremes of the fit window sit
            # where a log sweep has least energy per bin, and dividing two
            # sweeps there divides two small numbers. Reported 1.213 dB max
            # against a 0.154 dB whole-band fit and a +0.01 dB fitted offset
            # on 2026-08-09 -- a false alarm, and a check that cries wolf on a
            # good run stops being read.
            edge = freqs[out_of_band][int(np.argmax(residual))]
            median = float(np.median(residual))
            p95 = float(np.percentile(residual, 95))
            print(
                f"\n  Out-of-band residual: median {median:.3f} dB, "
                f"95th pct {p95:.3f} dB, max {residual.max():.3f} dB "
                f"at {edge:.0f} Hz"
            )
            if median > 0.25:
                print(
                    "    The whole out-of-band region has moved, not just a few\n"
                    "    bins. Something changed between the sweeps -- the mic\n"
                    "    moved, a level shifted, or the wrong band was edited.\n"
                    "    Treat the fit below as unreliable."
                )
            elif p95 > 0.5:
                print(
                    "    Typical agreement is fine; the tail is not. That is\n"
                    "    edge-of-sweep noise rather than anything moving, but\n"
                    "    narrow the fit range if it grows."
                )
            else:
                print("    Good -- the two sweeps differ only near the band.")
    else:
        print(f"\nSweep, median of {args.repeats} ...")
        measurement = capture_sweep(config, session)[1]
        measured_db = measurement.magnitude_dbfs(freqs)
        print(
            "\n  Single-sweep mode. The fit has to absorb the speaker, room and\n"
            "  interface response into its offset term, which it can only do if\n"
            "  they are flat. Prefer --differential."
        )

    f0, q, fitted_gain, offset, rms = fit_peaking(
        freqs, measured_db, args.freq, predicted_q, gain_db
    )

    print(f"\nFit over {lo:.0f}-{hi:.0f} Hz, {args.points} log-spaced points")
    print(f"  rms residual  {rms:7.3f} dB   <- is the SHAPE an RBJ peaking section?")

    q_lo, q_hi = _plausible_q_range()
    degenerate = not (q_lo <= q <= q_hi)
    if rms > 1.0:
        print(
            "    -> POOR FIT. The device may not be running a textbook peaking\n"
            "       biquad. Remapping Q will not fix this; inspect the curve\n"
            "       before changing any model."
        )
    elif degenerate:
        # A low residual is not evidence of shape on its own. A peaking
        # section with an absurd Q degenerates into a broad tilt and will
        # quietly fit almost anything smooth -- a synthetic shelf fits to
        # 0.13 dB rms at Q = 0.03. The device cannot store such a filter, so
        # a fit that needs one is describing our model bending, not the
        # firmware.
        print(
            f"    -> LOW RESIDUAL BUT DEGENERATE. Fitted Q {q:.3f} is outside "
            f"the\n       plausible range {q_lo:.2f}-{q_hi:.2f}, so the model has "
            f"flattened into\n       a tilt that fits anything smooth. Do NOT read "
            f"the residual as\n       confirming the shape. Check the band was "
            f"actually set, and that\n       the fit range brackets it."
        )
    else:
        print("    -> the RBJ peaking shape explains the measurement.")

    print("\n  Fitted parameters vs what was requested")
    print(
        f"    centre   {f0:9.1f} Hz   vs {args.freq:9.1f}   "
        f"({100 * (f0 - args.freq) / args.freq:+6.2f} %)"
    )
    print(
        f"    gain     {fitted_gain:+9.2f} dB   vs {gain_db:+9.2f}   "
        f"({fitted_gain - gain_db:+6.2f} dB)"
    )
    print(
        f"    Q        {q:9.3f}      vs {predicted_q:9.3f}   "
        f"({100 * (q - predicted_q) / predicted_q:+6.1f} %)"
    )
    if expected_offset_db:
        err = offset - expected_offset_db
        print(
            f"    offset   {offset:+9.2f} dB   vs {expected_offset_db:+9.2f}   "
            f"({err:+6.2f} dB)"
        )
        print(
            f"             the reference was taken {-expected_offset_db:.0f} dB "
            f"louder, so the offset should equal that\n"
            f"             exactly. It does to {abs(err):.2f} dB, which is the "
            f"flat path's level-linearity."
        )
    else:
        print(f"    offset   {offset:+9.2f} dB   (channel gain, not under test)")

    report_conventions(f0, q, fitted_gain, requested_octaves)

    prov = measurement.provenance
    print(
        f"\n  provenance: {prov.device} @ {prov.sample_rate_hz} Hz, "
        f"{prov.timestamp:%Y-%m-%d %H:%M}"
    )
    print(
        "\n  Record bw_raw, the fitted Q and the rms residual. The verdict "
        "comes from\n  how the error moves ACROSS bandwidths, not from any "
        "single run."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
