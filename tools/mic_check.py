"""Pre-flight for a microphone session: can these two devices run together?

A USB measurement microphone is on its own clock, so playback and capture
cannot share one. That is not a problem to solve, it is a fact to declare --
see :class:`tuner.audio.io.SplitDevices` -- but *whether the two streams will
open at a common rate at all* is a property of the host API and the Windows
sound settings, and it is worth knowing before an operator is sitting in a car.

Run it with nothing connected to the DSP outputs. **Nothing here emits a
stimulus**: the output stream carries silence, and the only thing measured is
the microphone's own idle noise floor::

    python tools/mic_check.py
    python tools/mic_check.py --seconds 5

Measured on this rig, 2026-08-13, Scarlett Solo + UMIK-1:

* one WASAPI duplex stream across the two -- ``Invalid sample rate``, always
* MME duplex -- works, by resampling, and MME already costs 2-5x the scatter
* two WASAPI streams at 48 kHz, output **exclusive** -- clean

The last is what ``SplitDevices`` does. Shared mode pins the interface to
44.1 kHz and the UMIK-1 is 48 kHz only, so exclusive mode on the output is
what gets both to one rate without touching Windows settings.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tuner.audio.io import SplitDevices, _extra_settings  # noqa: E402

#: Rates worth asking about. The DSP-408 is 48 kHz and cannot be changed, so a
#: session that runs at 48 kHz throughout has one fewer conversion in it.
RATES = (44_100, 48_000, 88_200, 96_000)


def resolve(
    fragment: str, want_input: bool, host_api: str = ""
) -> tuple[int, dict, str]:
    """Find one device by name fragment, or fail loudly about which ones match.

    Never by index. MME lists the host's default output first, so indices
    renumber when the default changes -- on this bench that once pointed a
    measurement at the PC's speakers while still capturing the correct input,
    yielding a smooth curve made of noise.
    """
    import sounddevice as sd

    matches = []
    for index, dev in enumerate(sd.query_devices()):
        api = sd.query_hostapis(dev["hostapi"])["name"]
        channels = dev["max_input_channels" if want_input else "max_output_channels"]
        if channels < 1:
            continue
        if host_api and host_api.lower() not in api.lower():
            continue
        label = f"{dev['name']}, {api}"
        if fragment.lower() in label.lower():
            matches.append((index, dev, api))
    if not matches:
        kind = "input" if want_input else "output"
        raise SystemExit(f"no {kind} device matches {fragment!r} on {host_api!r}")
    if len(matches) > 1:
        listing = "\n".join(f"    {i}: {d['name']} [{a}]" for i, d, a in matches)
        raise SystemExit(
            f"{fragment!r} is ambiguous across {len(matches)} devices:\n{listing}\n"
            f"Name the host API too, e.g. '{matches[0][1]['name']}, {matches[0][2]}'."
        )
    return matches[0]


def accepted_rates(index: int, want_input: bool, exclusive: bool) -> list[int]:
    import sounddevice as sd

    settings = _extra_settings(index, exclusive) if exclusive else None
    ok = []
    for rate in RATES:
        try:
            if want_input:
                sd.check_input_settings(
                    device=index,
                    channels=1,
                    samplerate=rate,
                    extra_settings=settings,
                )
            else:
                sd.check_output_settings(
                    device=index,
                    channels=1,
                    samplerate=rate,
                    extra_settings=settings,
                )
            ok.append(rate)
        except Exception:  # noqa: BLE001, PERF203 -- absence is the answer
            pass
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mic", default="Umik", help="capture device name fragment")
    ap.add_argument("--out", default="Scarlett", help="playback device fragment")
    ap.add_argument("--host-api", default="Windows WASAPI")
    ap.add_argument("--seconds", type=float, default=2.0)
    args = ap.parse_args()

    import sounddevice as sd

    mic_i, mic, mic_api = resolve(args.mic, True, args.host_api)
    out_i, out, out_api = resolve(args.out, False, args.host_api)
    print(f"capture   {mic_i:3d}  [{mic_api}]  {mic['name']}")
    print(f"playback  {out_i:3d}  [{out_api}]  {out['name']}")

    print("\nrates each device will accept")
    mic_shared = accepted_rates(mic_i, True, False)
    out_shared = accepted_rates(out_i, False, False)
    out_excl = accepted_rates(out_i, False, True) if "WASAPI" in out_api else []
    print(f"  capture,  shared     {mic_shared}")
    print(f"  playback, shared     {out_shared}")
    if out_excl:
        print(f"  playback, exclusive  {out_excl}")

    shared_common = sorted(set(mic_shared) & set(out_shared))
    excl_common = sorted(set(mic_shared) & set(out_excl))
    print(f"\n  common, shared output     {shared_common or 'NONE'}")
    print(f"  common, exclusive output  {excl_common or 'NONE'}")

    if not shared_common and not excl_common:
        print(
            "\nNo common rate. Nothing below will work, and the fix is in the\n"
            "Windows sound settings for one of these devices, not in the code."
        )
        return 2

    exclusive = not shared_common
    rate = (excl_common or shared_common)[-1]
    print(f"\nusing {rate} Hz, playback {'exclusive' if exclusive else 'shared'}")

    split = SplitDevices(
        output=f"{out['name']}, {out_api}",
        input=f"{mic['name']}, {mic_api}",
        output_exclusive=exclusive,
    )

    print(f"\nopening both streams for {args.seconds:.0f} s. Silence out.")
    frames: list[np.ndarray] = []
    n_in = int(mic["max_input_channels"])
    istream = sd.InputStream(
        device=split.input,
        samplerate=rate,
        channels=n_in,
        dtype="float32",
        callback=lambda d, f, t, s: frames.append(d.copy()),
    )
    ostream = sd.OutputStream(
        device=split.output,
        samplerate=rate,
        channels=1,
        dtype="float32",
        extra_settings=_extra_settings(split.output, exclusive),
        callback=lambda o, f, t, s: o.fill(0.0),
    )
    with istream, ostream:
        time.sleep(args.seconds)

    if not frames:
        print("  FAILED: both streams opened but the capture delivered no frames")
        return 2
    data = np.concatenate(frames)
    print(
        f"  OK: {data.shape[0]} frames = {data.shape[0] / rate:.3f} s "
        f"of {n_in}-channel capture"
    )
    for channel in range(data.shape[1]):
        column = data[:, channel]
        rms = float(np.sqrt(np.mean(column**2)))
        floor = 20 * np.log10(rms) if rms > 0 else float("-inf")
        print(
            f"    channel {channel}: idle floor {floor:7.1f} dBFS rms, "
            f"peak {float(np.max(np.abs(column))):.5f}"
        )
    if data.shape[1] > 1 and np.allclose(data[:, 0], data[:, 1]):
        print(
            "    channels are identical -- a mono microphone presented as "
            "stereo. Capture channel 0."
        )

    print("\nFor a measurement, hand this to capture_sweep:")
    print("    SplitDevices(")
    print(f"        output={split.output!r},")
    print(f"        input={split.input!r},")
    print(f"        output_exclusive={exclusive},")
    print("    )")
    print(
        f"    CaptureConfig(sample_rate_hz={rate}, device=<that>, "
        f"input_channels=(0,), loopback=None)"
    )
    print(
        "\nloopback=None is not a default to override. A split clock cannot\n"
        "carry a timing reference, so play_record refuses one -- every\n"
        "measurement taken this way is magnitude-only by construction."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
