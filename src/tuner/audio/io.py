"""Host-agnostic audio capture and playback.

Wraps PortAudio via ``sounddevice`` so the same code runs on Windows, Linux and
macOS unmodified -- desktop development now, embedded target later.

Playback and capture normally run on one device, so they share a sample clock.
Absolute stream latency does not need to be known or stable, because the
hardware loopback reference establishes t=0 within each capture. That is what
makes this usable without ASIO.

**Split-clock capture** -- output on one device, input on another -- is
supported through :class:`SplitDevices`, and only where phase was already
unavailable. This module used to say cross-device capture "must not be added",
on the grounds that clock drift destroys the phase relationships the chain
depends on. That reasoning is right and its conclusion was too broad: a USB
measurement microphone is on its own clock whatever PortAudio is asked to do,
so for a UMIK-1 there is no phase to destroy. What the rule protects is
enforced directly instead -- a split-clock capture cannot carry a loopback,
and :func:`play_record` refuses one.

Measured 2026-08-13 on this rig, with a UMIK-1 and a Scarlett Solo:

* **WASAPI cannot open one stream across the two.** ``Invalid sample rate`` at
  every rate, because shared mode pins each device to its own.
* **MME can**, and that is worse than failing: it succeeds by resampling, and
  MME is already measured at 2-5x the run-to-run scatter of WASAPI.
* **Two independent WASAPI streams work**, both at 48 kHz, with the Scarlett
  output opened in **exclusive** mode -- shared mode pins it to 44.1 kHz while
  the UMIK-1 is 48 kHz only.

The last point is the one worth keeping: a single duplex stream across two USB
devices is an *illusion of a shared clock*. Two independent streams are not a
fallback, they are the honest description of what the hardware is doing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from ..safety.limits import DEFAULT_CEILING_DBFS, SafetyViolation

#: Seconds the capture stream runs before playback starts. Covers the output
#: stream's own start-up, so the stimulus cannot be clipped at its head by a
#: capture that was not yet running.
SPLIT_LEAD_IN_S = 0.25

#: Frames the capture may fall short by before it is treated as a failure
#: rather than an edge. One buffer is ordinary -- the capture closes a moment
#: after the last frame is played. Anything much larger means the window
#: landed past the end of the recording, and the reason is almost always a
#: slow output-stream start.
MAX_SPLIT_SHORTFALL = 2048


@dataclass(frozen=True)
class LoopbackConfig:
    """Hardware loopback wiring: one output fed back to one input.

    The loopback output carries the *same* stimulus as the channel under test,
    so deconvolving it yields the reference arrival that defines t=0. Without
    it, delay and phase results are unavailable -- see the timing-reference
    rule in CLAUDE.md.

    Channel numbers are 0-based, matching the rest of this project.
    """

    output_channel: int
    input_channel: int


@dataclass(frozen=True)
class SplitDevices:
    """Playback and capture on two devices with **independent sample clocks**.

    The case this exists for: a USB measurement microphone. A UMIK-1 has its
    own crystal and cannot be slaved to the interface, so no arrangement of
    streams makes the two share a clock -- asking PortAudio for one duplex
    stream across them either fails (WASAPI) or silently resamples (MME).

    Declaring this is therefore not a workaround, it is an accurate statement
    about the hardware. What follows from it is enforced rather than trusted:
    :func:`play_record` **refuses a loopback** on a split-clock capture, so
    every measurement taken this way is magnitude-only by construction and the
    timing-reference rule cannot be violated by accident.

    Attributes:
        output: Playback device. Use a host-API-qualified name, never an
            index -- MME lists the host's default output first, so indices
            renumber when the default changes.
        input: Capture device, same rule.
        output_exclusive: Open the output in WASAPI exclusive mode. Needed on
            this rig: shared mode pins the Scarlett to 44.1 kHz while the
            UMIK-1 is 48 kHz only, and both streams must run at one rate for
            the capture to be interpretable without resampling.
        input_exclusive: The same for the capture device.
    """

    output: str | int
    input: str | int
    output_exclusive: bool = False
    input_exclusive: bool = False


def _extra_settings(device: str | int, exclusive: bool) -> object | None:
    """WASAPI exclusive-mode settings, or None when not applicable."""
    if not exclusive:
        return None
    import sounddevice as sd

    info = sd.query_devices(device)
    api = sd.query_hostapis(info["hostapi"])["name"]
    if "WASAPI" not in api:
        raise ValueError(
            f"exclusive mode was asked for on {info['name']!r}, which is a "
            f"{api} device. Exclusive mode is a WASAPI concept; name the "
            f"WASAPI instance of the device instead."
        )
    return sd.WasapiSettings(exclusive=True)


def _device_channels(device: str | int, direction: str) -> int:
    """How many channels the device actually has, in the given direction.

    Needed because :func:`sd.playrec`'s ``output_mapping`` does not exist on
    the raw stream API this path uses. There, the buffer's own width *is* the
    channel count, so it has to match the hardware rather than the caller's
    highest channel index.
    """
    import sounddevice as sd

    info = sd.query_devices(device)
    return int(info[f"max_{direction}_channels"])


def _widen(playback: np.ndarray, channels: int) -> np.ndarray:
    """Pad a playback buffer out to the device's channel count.

    **This is a correctness fix, not tidiness.** A WASAPI stream in exclusive
    mode performs no format conversion at all -- that is what exclusive mode
    *means*, and it is why this project uses it to reach 48 kHz. Hand such a
    stream a buffer narrower than the device's native format and the driver
    reads the samples as interleaved frames of the format it does have. On a
    two-channel device, a mono buffer is therefore consumed at two samples per
    frame: **every stimulus plays at double speed, one octave high.**

    Measured on the bench 2026-08-13, a 1000 Hz tone through this path:

        buffer channels=1  ->  heard 1984.2 Hz   (-72.0 dBFS)
        buffer channels=2  ->  heard 1000.0 Hz   (-64.4 dBFS)

    Nothing else reports it. Both streams' wall-clock rates measure correct,
    the level is untouched, no frames are dropped, and the capture is full of
    a clean steady tone -- of the wrong frequency. The level being untouched
    is why this is not a safety-limiter failure, and the spectrum being
    shifted is why it is still a safety-relevant one: a sweep band-limited for
    a particular driver arrives an octave away from where it was aimed.
    """
    if channels <= playback.shape[1]:
        return playback
    wide = np.zeros((playback.shape[0], channels), dtype=playback.dtype)
    wide[:, : playback.shape[1]] = playback
    return wide


def _play_record_split(
    playback: np.ndarray,
    capture_channels: list[int],
    sample_rate_hz: int,
    split: SplitDevices,
) -> np.ndarray:
    """Two streams, two clocks, one capture window aligned to the playback.

    The capture starts first and stops last, so the stimulus cannot be clipped
    by a stream that was still starting. The returned array is then trimmed to
    exactly ``len(playback)`` frames from the moment playback began, which
    keeps the contract every caller of :func:`play_record` already relies on.

    That trim point is approximate -- it is read from the input stream's own
    frame count at the instant the write begins, so it is good to about one
    buffer. **Nothing downstream needs it to be better**, because a split
    clock has no timing reference by construction and the deconvolution's
    zero-delay point is already an arbitrary offset.
    """
    import sounddevice as sd

    frames: list[np.ndarray] = []

    # Both widths come from the hardware, not from the caller's channel
    # indices. See `_widen`: in exclusive mode a narrow buffer is not padded,
    # it is reinterpreted, and the stimulus comes back an octave high.
    n_out = max(_device_channels(split.output, "output"), playback.shape[1])
    n_in = max(_device_channels(split.input, "input"), max(capture_channels) + 1)
    playback = _widen(playback, n_out)

    def on_input(indata, _frames, _time, _status) -> None:  # noqa: ANN001
        frames.append(indata.copy())

    istream = sd.InputStream(
        device=split.input,
        samplerate=sample_rate_hz,
        channels=n_in,
        dtype="float32",
        callback=on_input,
        extra_settings=_extra_settings(split.input, split.input_exclusive),
    )
    ostream = sd.OutputStream(
        device=split.output,
        samplerate=sample_rate_hz,
        channels=playback.shape[1],
        dtype="float32",
        extra_settings=_extra_settings(split.output, split.output_exclusive),
    )

    with istream:
        time.sleep(SPLIT_LEAD_IN_S)
        with ostream:
            started_at = sum(len(f) for f in frames)
            ostream.write(playback)
            # write() returns once the buffer is handed over, not once it has
            # been heard. Hold the capture open for the stream's own latency
            # so the tail of the stimulus is not truncated by close().
            time.sleep(float(ostream.latency) + 0.05)

    if not frames:
        raise SafetyViolation(
            f"the capture device {split.input!r} delivered no frames while "
            f"the stimulus played. The measurement is empty, and deconvolving "
            f"an empty capture yields a smooth, plausible, entirely wrong "
            f"curve."
        )

    captured = np.concatenate(frames)
    window = captured[started_at : started_at + playback.shape[0]]
    short = playback.shape[0] - window.shape[0]

    # A shortfall of about a buffer is the ordinary case: the capture is
    # closed a moment after the last frame is played. Anything larger means
    # the window landed past the end of what was captured, and the usual
    # cause is the output stream taking a long time to start -- a cold WASAPI
    # exclusive open can run into seconds, and `started_at` is read before
    # `write()` returns.
    #
    # **This must raise rather than pad.** Found on the bench 2026-08-13, the
    # first time this path met hardware: the run silently returned an
    # all-zero buffer, and an all-zero capture deconvolves into a smooth,
    # entirely plausible frequency response. That is the exact failure this
    # project's rig-verification rules exist to prevent, reintroduced by the
    # convenience of padding.
    if short > max(MAX_SPLIT_SHORTFALL, playback.shape[0] // 100):
        raise SafetyViolation(
            f"the capture window landed past the end of the recording: "
            f"{short} of {playback.shape[0]} frames missing, starting at "
            f"{started_at} of {captured.shape[0]} captured. The output "
            f"stream almost certainly took a long time to start. Nothing "
            f"here is measurable -- an all-zero capture deconvolves into a "
            f"smooth and completely wrong curve, so this refuses rather "
            f"than padding."
        )
    if short > 0:
        window = np.vstack([window, np.zeros((short, window.shape[1]))])
    return np.asarray(window[:, capture_channels], dtype=np.float64)


def play_record(
    stimulus: np.ndarray,
    output_channel: int,
    input_channels: list[int],
    sample_rate_hz: int,
    device: str | int | tuple[int, int] | SplitDevices | None = None,
    loopback: LoopbackConfig | None = None,
    tail_s: float = 1.0,
    max_peak_dbfs: float = DEFAULT_CEILING_DBFS,
) -> np.ndarray:
    """Play ``stimulus`` on one output while capturing ``input_channels``.

    ``stimulus`` must already have passed through :mod:`tuner.safety`. As a
    second line of defence this function *also* refuses anything peaking above
    ``max_peak_dbfs`` -- rule 1 says no stimulus reaches an output device
    without a safety check, and a belt-and-braces check here costs nothing
    while catching a forgotten call site.

    ``tail_s`` extends the capture past the end of the stimulus so the decay
    is not truncated.

    Args:
        stimulus: 1-D, already levelled.
        output_channel: 0-based output to drive.
        input_channels: 0-based inputs to capture, in the order returned.
        sample_rate_hz: Rate for both directions.
        device: PortAudio device, or ``(input_device, output_device)`` on the
            same physical hardware, or a :class:`SplitDevices` when playback
            and capture are on two devices with independent clocks. A tuple
            still means one clock; ``SplitDevices`` is the declaration that
            there are two, and it forbids a loopback.
        loopback: If given, the same stimulus is also sent to
            ``loopback.output_channel`` and that input is captured.
        tail_s: Silence appended after the stimulus.
        max_peak_dbfs: Refuse stimuli peaking above this.

    Returns:
        Array shaped ``(n_samples, len(input_channels))``, float64.
    """
    import sounddevice as sd  # deferred: importing PortAudio has a real cost

    stimulus = np.asarray(stimulus, dtype=np.float64).squeeze()
    if stimulus.ndim != 1:
        raise ValueError("stimulus must be 1-D")
    if not input_channels:
        raise ValueError("need at least one input channel")
    if tail_s < 0:
        raise ValueError("tail_s must be non-negative")

    peak = float(np.max(np.abs(stimulus))) if stimulus.size else 0.0
    if peak > 0:
        peak_dbfs = 20.0 * np.log10(peak)
        if peak_dbfs > max_peak_dbfs:
            raise SafetyViolation(
                f"stimulus peaks at {peak_dbfs:.1f} dBFS, above the "
                f"{max_peak_dbfs:.1f} dBFS limit for this call. Level it "
                f"through tuner.safety.apply() first."
            )

    padded = np.concatenate([stimulus, np.zeros(int(round(tail_s * sample_rate_hz)))])

    if isinstance(device, SplitDevices) and loopback is not None:
        # The whole point of SplitDevices. Two clocks cannot establish a
        # common t=0, so a "loopback" captured across them is a reference to
        # nothing -- and it would set has_timing_reference, which is what
        # unlocks every delay and phase figure in the project.
        raise ValueError(
            "a loopback cannot be captured across two devices: they run on "
            "independent clocks, so the reference arrival does not define t=0 "
            "for the measurement. Either put the loopback on the same device "
            "as the measurement input, or drop it and accept a "
            "magnitude-only capture -- which is all a USB microphone can give."
        )

    out_channels = [output_channel]
    if loopback is not None:
        if loopback.output_channel == output_channel:
            raise ValueError(
                "loopback output must differ from the channel under test; "
                "otherwise the reference and the measurement are the same signal"
            )
        out_channels.append(loopback.output_channel)

    playback = np.zeros((padded.size, max(out_channels) + 1), dtype=np.float32)
    for ch in out_channels:
        playback[:, ch] = padded

    capture_channels = list(input_channels)
    if loopback is not None and loopback.input_channel not in capture_channels:
        capture_channels.append(loopback.input_channel)

    if isinstance(device, SplitDevices):
        return _play_record_split(playback, capture_channels, sample_rate_hz, device)

    # sounddevice channel mappings are 1-based.
    recorded = sd.playrec(
        playback,
        samplerate=sample_rate_hz,
        device=device,
        input_mapping=[c + 1 for c in capture_channels],
        output_mapping=[c + 1 for c in out_channels],
        blocking=True,
        dtype="float32",
    )

    return np.asarray(recorded, dtype=np.float64)


def loopback_index(
    input_channels: list[int],
    loopback: LoopbackConfig | None,
) -> int | None:
    """Column in a :func:`play_record` result holding the loopback reference.

    Returns None when no loopback was configured -- the caller must then treat
    the measurement as magnitude-only.
    """
    if loopback is None:
        return None
    channels = list(input_channels)
    if loopback.input_channel not in channels:
        channels.append(loopback.input_channel)
    return channels.index(loopback.input_channel)
