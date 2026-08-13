"""Rig verification: checks that must pass before a measurement is believable.

These are not diagnostics to reach for when something looks wrong. They are
preconditions, because the failures they catch do not look wrong -- they
produce smooth, plausible, entirely incorrect curves.

The motivating case is documented in docs/hardware.md: Windows applied speech
noise-suppression to a capture endpoint, an expander whose gain varied by 80 dB
with input level. It manufactured a convincing 70 dB/octave low-frequency
rolloff that did not exist, and four separate frequency-response measurements
were quietly corrupted before a level sweep exposed it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from ..audio.io import play_record
from ..safety.limits import ChannelLimit, apply

#: Levels swept when checking linearity. Spans the range a measurement might
#: plausibly use, low enough to trip a noise gate if one is present.
DEFAULT_TEST_LEVELS_DBFS = (-40.0, -30.0, -20.0, -12.0, -6.0)

#: Frequencies probed. Chosen to straddle the voice band, because speech
#: processing is frequency-selective -- it opened at 3 kHz a full 8 dB before
#: it opened at 300 Hz on the interim rig.
DEFAULT_TEST_FREQS_HZ = (300.0, 1000.0, 3000.0)

#: A linear path holds gain constant. Anything beyond this is processing.
DEFAULT_TOLERANCE_DB = 1.0

#: Module-level singleton; frozen, so sharing it as a default is safe.
_DEFAULT_LIMIT = ChannelLimit()


#: A tone must sit at least this far above the quietest measured gain before
#: it counts as carrying signal rather than noise floor. Through a filtered
#: channel an out-of-band tone reads its own noise floor, and that floor moves
#: with stimulus level in a way that mimics compression exactly.
DEFAULT_MIN_TONE_HEADROOM_DB = 40.0

#: Fewer usable tones than this and the check cannot conclude anything. Two is
#: the minimum that can disagree; one can only fail to contradict linearity.
MIN_USABLE_TONES = 2


class NonLinearPath(RuntimeError):
    """Raised when measured gain depends on level.

    Something in the chain is compressing, expanding, gating or limiting.
    Frequency-response measurements through it are not trustworthy at any
    single level.
    """


class IndeterminateLinearity(RuntimeError):
    """Raised when the check could not conclude either way.

    Distinct from :class:`NonLinearPath` on purpose. A filtered channel can
    leave too few tones in its passband to establish level-independence, and
    "we could not tell" is a third outcome, not a pass. Collapsing it into a
    pass is how a compressor survives a check that appeared to run.
    """


@dataclass(frozen=True)
class LinearityResult:
    """Measured gain at each (frequency, level) combination."""

    freqs_hz: tuple[float, ...]
    levels_dbfs: tuple[float, ...]
    #: Shape (len(freqs_hz), len(levels_dbfs)), gain in dB.
    gain_db: np.ndarray

    def usable(
        self, min_headroom_db: float = DEFAULT_MIN_TONE_HEADROOM_DB
    ) -> np.ndarray:
        """Boolean mask of tones that carry signal rather than noise floor.

        A tone in a crossover's stopband reads whatever the noise floor is
        doing. That floor does not scale with stimulus level, so its apparent
        gain varies with level exactly as a compressor's would -- measured on
        the bench, a 3 kHz tone through a 450 Hz low-pass produced a 1.65 dB
        "gain variation" on a path whose passband tones agreed to 0.02 dB.

        Judged relative to the loudest tone, because absolute level depends on
        channel gain and interface gain, neither of which this check knows.
        """
        loudest = self.gain_db.max()
        return self.gain_db.max(axis=1) > loudest - min_headroom_db

    def spread_db_of(self, mask: np.ndarray) -> float:
        rows = self.gain_db[mask]
        if rows.size == 0:
            return 0.0
        return float(np.max(rows.max(axis=1) - rows.min(axis=1)))

    @property
    def spread_db(self) -> float:
        """Largest gain variation across levels, over the usable tones."""
        return self.spread_db_of(self.usable())

    @property
    def spread_db_all_tones(self) -> float:
        """Spread including tones in the noise floor. Diagnostic only."""
        return float(np.max(self.gain_db.max(axis=1) - self.gain_db.min(axis=1)))

    @property
    def is_linear(self) -> bool:
        return self.spread_db <= DEFAULT_TOLERANCE_DB

    def report(self) -> str:
        lines = ["level  | " + "  ".join(f"{f:>8.0f} Hz" for f in self.freqs_hz)]
        for j, level in enumerate(self.levels_dbfs):
            row = "  ".join(
                f"{self.gain_db[i, j]:>11.2f}" for i in range(len(self.freqs_hz))
            )
            lines.append(f"{level:>6.0f} | {row}")
        mask = self.usable()
        dropped = [
            f"{f:.0f} Hz" for f, ok in zip(self.freqs_hz, mask, strict=True) if not ok
        ]
        if dropped:
            lines.append(
                f"excluded as noise floor: {', '.join(dropped)} "
                f"({int(mask.sum())} of {len(self.freqs_hz)} tones usable)"
            )
        lines.append(f"spread across levels: {self.spread_db:.2f} dB")
        return "\n".join(lines)


def measure_level_linearity(
    sample_rate_hz: int,
    output_channel: int,
    input_channel: int,
    device: str | int | tuple[int, int] | None = None,
    limit: ChannelLimit = _DEFAULT_LIMIT,
    freqs_hz: tuple[float, ...] = DEFAULT_TEST_FREQS_HZ,
    levels_dbfs: tuple[float, ...] = DEFAULT_TEST_LEVELS_DBFS,
    duration_s: float = 1.5,
    settle_s: float = 0.4,
) -> LinearityResult:
    """Measure gain at several levels and frequencies.

    Levels above the channel's ceiling are skipped rather than clipped, so the
    check never becomes a reason to exceed a safety limit.

    ``settle_s`` of each capture is discarded, because a compressor or gate
    that does exist needs time to act and would otherwise be averaged away.
    """
    usable = tuple(lvl for lvl in levels_dbfs if lvl <= limit.ceiling_dbfs)
    if len(usable) < 2:
        raise ValueError(
            f"need at least 2 test levels at or below the channel ceiling "
            f"({limit.ceiling_dbfs:.1f} dBFS); got {usable}"
        )

    gains = np.empty((len(freqs_hz), len(usable)), dtype=np.float64)
    n = int(round(duration_s * sample_rate_hz))
    t = np.arange(n) / sample_rate_hz

    for i, freq in enumerate(freqs_hz):
        tone = np.sin(2.0 * np.pi * freq * t)
        for j, level in enumerate(usable):
            stimulus = apply(tone, level, limit)
            recorded = play_record(
                stimulus,
                output_channel=output_channel,
                input_channels=[input_channel],
                sample_rate_hz=sample_rate_hz,
                device=device,
                tail_s=0.0,
                max_peak_dbfs=limit.ceiling_dbfs,
            )
            y = recorded[int(settle_s * sample_rate_hz) :, 0]
            window = np.hanning(y.size)
            spectrum = np.fft.rfft(y * window)
            bins = np.fft.rfftfreq(y.size, 1.0 / sample_rate_hz)
            amplitude = np.abs(spectrum[np.argmin(np.abs(bins - freq))]) / (
                np.sum(window) / 2.0
            )
            gains[i, j] = 20.0 * np.log10(amplitude + 1e-30) - level

    return LinearityResult(freqs_hz=tuple(freqs_hz), levels_dbfs=usable, gain_db=gains)


def require_linear_path(
    result: LinearityResult,
    tolerance_db: float = DEFAULT_TOLERANCE_DB,
    min_usable_tones: int = MIN_USABLE_TONES,
) -> None:
    """Raise unless gain is measurably level-independent.

    Three outcomes, not two:

    * returns -- gain is constant across level on enough tones to mean it;
    * :class:`NonLinearPath` -- gain varies with level;
    * :class:`IndeterminateLinearity` -- too few tones carried signal to
      conclude anything, which is what happens when the default full-range
      tones are used on a crossover-filtered channel.

    Tones sitting in the noise floor are excluded before judging, because they
    are not merely noisy but **uninformative**: a compressor acting in the
    passband is invisible to a stopband tone at any SNR. Excluding them is
    therefore not a sensitivity tweak, it is the difference between measuring
    the path and measuring its noise.
    """
    mask = result.usable()
    usable = int(mask.sum())
    if usable < min_usable_tones:
        raise IndeterminateLinearity(
            f"only {usable} of {len(result.freqs_hz)} test tones carried "
            f"signal; {min_usable_tones} are needed before level-independence "
            f"means anything. This is the expected result of probing a "
            f"crossover-filtered channel with full-range tones -- re-run with "
            f"frequencies inside this channel's passband.\n\n"
            f"{result.report()}"
        )
    if result.spread_db > tolerance_db:
        raise NonLinearPath(
            f"gain varies by {result.spread_db:.1f} dB across input level "
            f"(tolerance {tolerance_db:.1f} dB). Something in the chain is "
            f"gating, compressing or limiting; frequency-response "
            f"measurements through it are not trustworthy.\n\n"
            f"{result.report()}\n\n"
            f"On Windows, check the capture endpoint's audio enhancements. "
            f"In a vehicle, suspect head-unit loudness compensation or an "
            f"amplifier limiter."
        )


#: Idle floor must sit at least this far below the stimulus level. Swept-sine
#: deconvolution rejects uncorrelated interference well, so this is deliberately
#: not a tight bound -- measured on the interim rig, interference only 6 dB
#: below the stimulus in-band moved a crossover corner by 6% while still
#: producing a smooth, plausible curve. 40 dB leaves generous margin over that.
DEFAULT_MIN_SNR_DB = 40.0


class NoisyPath(RuntimeError):
    """Raised when the capture is not quiet enough with no stimulus playing.

    Something is reaching the input that the measurement did not put there:
    other audio on the same output endpoint, a ground loop, or interference.
    """


@dataclass(frozen=True)
class IdleNoiseResult:
    """What arrives at the input while nothing is being played."""

    rms_dbfs: float
    peak_dbfs: float
    sample_rate_hz: int
    #: (low_hz, high_hz) -> level in dBFS, for locating the source.
    bands_dbfs: dict[tuple[float, float], float]

    def snr_db(self, level_dbfs: float) -> float:
        """Headroom between a stimulus at ``level_dbfs`` and this floor."""
        return level_dbfs - self.rms_dbfs

    def report(self) -> str:
        lines = [
            f"idle rms  {self.rms_dbfs:>8.2f} dBFS",
            f"idle peak {self.peak_dbfs:>8.2f} dBFS",
        ]
        for (lo, hi), value in self.bands_dbfs.items():
            lines.append(f"  {lo:>6.0f} - {hi:<6.0f} Hz  {value:>8.2f} dBFS")
        return "\n".join(lines)


#: Bands reported by the idle check. Chosen to separate the usual suspects:
#: mains hum and its harmonics, program material leaking from the host, and
#: wideband hiss.
DEFAULT_NOISE_BANDS = ((20.0, 150.0), (150.0, 6000.0), (6000.0, 20000.0))


def analyze_idle_noise(
    samples: np.ndarray,
    sample_rate_hz: int,
    bands_hz: tuple[tuple[float, float], ...] = DEFAULT_NOISE_BANDS,
) -> IdleNoiseResult:
    """Summarise a silent capture. Pure -- no I/O.

    Split out from :func:`measure_idle_noise` so the capture path can reuse its
    own already-captured silence rather than playing a second one, and so the
    analysis is testable without stubbing an audio layer.
    """
    y = np.asarray(samples, dtype=np.float64)
    window = np.hanning(y.size)
    spectrum = np.abs(np.fft.rfft(y * window)) / (np.sum(window) / 2.0)
    bins = np.fft.rfftfreq(y.size, 1.0 / sample_rate_hz)

    def to_db(value: float) -> float:
        return float(20.0 * np.log10(value + 1e-30))

    bands = {}
    for lo, hi in bands_hz:
        selected = (bins >= lo) & (bins <= hi)
        bands[(lo, hi)] = to_db(float(np.sqrt(np.sum(spectrum[selected] ** 2))))

    return IdleNoiseResult(
        rms_dbfs=to_db(float(np.sqrt(np.mean(y**2)))),
        peak_dbfs=to_db(float(np.max(np.abs(y)))),
        sample_rate_hz=sample_rate_hz,
        bands_dbfs=bands,
    )


def measure_idle_noise(
    sample_rate_hz: int,
    input_channel: int,
    output_channel: int = 0,
    device: str | int | tuple[int, int] | None = None,
    duration_s: float = 1.0,
    bands_hz: tuple[tuple[float, float], ...] = DEFAULT_NOISE_BANDS,
) -> IdleNoiseResult:
    """Capture with no stimulus and report what arrives anyway.

    A silent stimulus is still played, so the capture runs through exactly the
    same device path, buffering and channel mapping as a real measurement --
    a floor measured through a different path is not the floor that will
    contaminate the sweep.
    """
    n = int(round(duration_s * sample_rate_hz))
    recorded = play_record(
        np.zeros(n, dtype=np.float64),
        output_channel=output_channel,
        input_channels=[input_channel],
        sample_rate_hz=sample_rate_hz,
        device=device,
        tail_s=0.0,
        max_peak_dbfs=0.0,
    )
    return analyze_idle_noise(np.asarray(recorded)[:, 0], sample_rate_hz, bands_hz)


def require_quiet_path(
    result: IdleNoiseResult,
    level_dbfs: float,
    min_snr_db: float = DEFAULT_MIN_SNR_DB,
) -> None:
    """Raise unless the idle floor sits ``min_snr_db`` below ``level_dbfs``.

    The companion to :func:`require_linear_path`, and for the same reason: the
    failure it catches does not look like a failure. Interference corrupts a
    swept-sine measurement into a curve that is still smooth and still shaped
    roughly right, so there is nothing to notice afterwards.

    Two real cases from the bench, both of which this would have caught before
    a single sweep was run: a USB ground path that raised the floor 43 dB, and
    a video playing on the host's default output -- which happened to be the
    same interface -- that moved a measured crossover corner by 6%.
    """
    snr = result.snr_db(level_dbfs)
    if snr < min_snr_db:
        raise NoisyPath(
            f"idle noise floor is {result.rms_dbfs:.1f} dBFS, only "
            f"{snr:.1f} dB below the {level_dbfs:.1f} dBFS stimulus "
            f"(need {min_snr_db:.1f} dB). Something is reaching the input "
            f"that the measurement did not put there.\n\n"
            f"{result.report()}\n\n"
            f"Check: other audio playing on the same output device (is the "
            f"interface the host's default?), a ground loop from a USB or "
            f"control cable sharing the interface's ground, or mains "
            f"interference. Energy concentrated at 50/60 Hz and harmonics "
            f"points at a ground loop; broadband energy in 150 Hz - 6 kHz "
            f"points at program material."
        )


#: A probe at the measurement level must lift the input at least this far above
#: the idle floor. Generous: a working chain clears it by tens of dB, and the
#: failure being caught is total silence, not a few dB of loss.
DEFAULT_MIN_RESPONSE_DB = 12.0


class SilentPath(RuntimeError):
    """Raised when the stimulus does not appear at the input at all.

    The measurement is addressing something other than the rig: the wrong
    device, an unplugged cable, a muted output.
    """


def rms_dbfs(samples: np.ndarray) -> float:
    """RMS of ``samples`` in dBFS."""
    y = np.asarray(samples, dtype=np.float64)
    return float(20.0 * np.log10(np.sqrt(np.mean(y**2)) + 1e-30))


def require_signal_response(
    idle: IdleNoiseResult,
    probe_levels_dbfs: Sequence[float],
    captured_rms_dbfs: Sequence[float],
    min_response_db: float = DEFAULT_MIN_RESPONSE_DB,
    channel: int | None = None,
) -> None:
    """Raise unless the stimulus actually reaches the input.

    The third precondition, and the one whose absence was most expensive. A
    capture that receives nothing still passes every other check: there is no
    clipping, no DC, the noise floor is clean, and deconvolving noise yields a
    smooth curve with plausible-looking features. On the bench this happened
    because MME renumbers its device indices when the Windows default output
    changes -- the sweep went to the PC's speakers while the correct input was
    captured, and the result looked like a measurement.

    Two conditions, because "loud" alone is not enough:

    1. The loudest probe must lift the input clear of the idle floor. Catches
       nothing arriving.
    2. The input must *rise* between the quietest and loudest probe. Catches a
       constant interferer being mistaken for signal -- that lifts the level
       without responding to us.
    """
    if not captured_rms_dbfs:
        return
    where = "" if channel is None else f" on channel {channel}"

    loudest = max(captured_rms_dbfs)
    response = loudest - idle.rms_dbfs
    if response < min_response_db:
        raise SilentPath(
            f"the stimulus is not reaching the input{where}: the loudest probe "
            f"({max(probe_levels_dbfs):.1f} dBFS) lifted the capture to "
            f"{loudest:.1f} dBFS, only {response:.1f} dB above the "
            f"{idle.rms_dbfs:.1f} dBFS idle floor (need {min_response_db:.1f} "
            f"dB).\n\n"
            f"Check the output device first. Device *indices* are not stable: "
            f"MME lists the host's default output first, so its numbering "
            f"shifts whenever the default changes. Select devices by name, "
            f"host-API qualified, rather than by index. Then check the output "
            f"cable, and that the channel is not muted."
        )

    if len(captured_rms_dbfs) >= 2:
        quietest_probe = int(np.argmin(probe_levels_dbfs))
        loudest_probe = int(np.argmax(probe_levels_dbfs))
        rise = captured_rms_dbfs[loudest_probe] - captured_rms_dbfs[quietest_probe]
        if rise <= 0.0:
            raise SilentPath(
                f"the input{where} does not respond to the stimulus level: "
                f"raising the probe from "
                f"{probe_levels_dbfs[quietest_probe]:.1f} to "
                f"{probe_levels_dbfs[loudest_probe]:.1f} dBFS changed the "
                f"capture by {rise:+.1f} dB. The level reaching the input is "
                f"not coming from us -- suspect a constant interferer, or a "
                f"capture addressing a different device than the playback."
            )
