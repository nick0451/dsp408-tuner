"""Playback and capture on two devices with independent sample clocks.

The case: a UMIK-1. It has its own crystal and cannot be slaved to the
interface, so no arrangement of streams makes the two share a clock. Measured
on this rig 2026-08-13 -- WASAPI refuses one duplex stream across them at
every rate, MME accepts it only by resampling, and two independent WASAPI
streams run cleanly at 48 kHz with the interface output in exclusive mode.

``sounddevice`` is faked here: the real thing needs hardware, and the suite
must pass with none attached.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import numpy as np
import pytest

from tuner.audio.io import (
    SPLIT_LEAD_IN_S,
    LoopbackConfig,
    SplitDevices,
    play_record,
)
from tuner.safety.limits import SafetyViolation


@dataclass
class FakeStream:
    """Enough of a PortAudio stream to drive the split path."""

    owner: FakePortAudio
    kind: str
    device: object
    samplerate: int
    channels: int
    callback: object = None
    extra_settings: object = None
    latency: float = 0.01

    def __enter__(self) -> FakeStream:
        self.owner.opened.append(self)
        if self.kind == "input":
            self.owner.deliver(self)
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def write(self, data: np.ndarray) -> None:
        self.owner.written.append(np.asarray(data).copy())
        # A real device keeps capturing while it plays -- but not necessarily
        # for as long as the playback, which is the whole point of
        # `frames_during_playback`.
        during = self.owner.frames_during_playback
        for stream in self.owner.opened:
            if stream.kind == "input":
                self.owner.deliver(
                    stream, frames=len(data) if during is None else during
                )


@dataclass
class FakePortAudio:
    """Records what was asked for, and hands back a synthetic capture."""

    input_frames_before_playback: int = 512
    #: Frames delivered while the playback runs. ``None`` means "as many as
    #: were played", which is the healthy case. A smaller number models a
    #: capture that ended early -- what a slow output-stream start produces.
    frames_during_playback: int | None = None
    silent: bool = False
    #: The hardware's own channel counts. Two, like the Scarlett Solo and the
    #: UMIK-1, because a fake that reports exactly what the caller asked for
    #: cannot express a format mismatch -- and a format mismatch in exclusive
    #: mode is what played every stimulus an octave high on 2026-08-13.
    device_output_channels: int = 2
    device_input_channels: int = 2
    devices: dict = field(default_factory=dict)
    opened: list = field(default_factory=list)
    written: list = field(default_factory=list)
    _tick: int = 0

    class WasapiSettings:
        def __init__(self, exclusive: bool = False) -> None:
            self.exclusive = exclusive

    def deliver(self, stream: FakeStream, frames: int | None = None) -> None:
        if self.silent:
            return
        n = self.input_frames_before_playback if frames is None else frames
        if n == 0:
            return
        self._tick += 1
        # Each delivered block carries a distinct value, so the lead-in can
        # be told apart from the playback window in the returned array.
        block = np.full((n, stream.channels), float(self._tick), dtype=np.float32)
        stream.callback(block, n, None, None)

    def InputStream(self, **kw: object) -> FakeStream:  # noqa: N802
        return FakeStream(
            self,
            "input",
            kw["device"],
            kw["samplerate"],
            kw["channels"],
            kw.get("callback"),
            kw.get("extra_settings"),
        )

    def OutputStream(self, **kw: object) -> FakeStream:  # noqa: N802
        return FakeStream(
            self,
            "output",
            kw["device"],
            kw["samplerate"],
            kw["channels"],
            kw.get("callback"),
            kw.get("extra_settings"),
        )

    def query_devices(self, device: object) -> dict:
        info = {
            "name": str(device),
            "hostapi": 0,
            "max_output_channels": self.device_output_channels,
            "max_input_channels": self.device_input_channels,
        }
        info.update(self.devices.get(device, {}))
        return info

    def query_hostapis(self, index: int) -> dict:
        return {"name": "Windows WASAPI" if index == 0 else "MME"}


@pytest.fixture
def fake_pa(monkeypatch):
    pa = FakePortAudio()
    monkeypatch.setitem(sys.modules, "sounddevice", pa)
    pa.slept = []
    monkeypatch.setattr("tuner.audio.io.time.sleep", pa.slept.append)
    return pa


SPLIT = SplitDevices(
    output="Speakers (Scarlett Solo USB), Windows WASAPI",
    input="Microphone (Umik-1), Windows WASAPI",
    output_exclusive=True,
)


def a_stimulus(n: int = 4096) -> np.ndarray:
    return 0.05 * np.sin(np.linspace(0.0, 200.0, n))


class TestTheLoopbackRefusal:
    """Why ``SplitDevices`` is a type and not a tuple."""

    def _with_loopback(self):
        return {
            "output_channel": 0,
            "input_channels": [0],
            "sample_rate_hz": 48_000,
            "device": SPLIT,
            "loopback": LoopbackConfig(output_channel=1, input_channel=1),
        }

    def test_a_loopback_across_two_clocks_is_refused(self, fake_pa):
        """A reference captured on a clock of its own is a reference to nothing.

        Accepting one would set ``has_timing_reference``, which is what
        unlocks every delay and phase figure in the project. Refused at the
        call rather than warned about in a docstring.
        """
        with pytest.raises(ValueError, match="independent clocks"):
            play_record(a_stimulus(), **self._with_loopback())

    def test_the_refusal_emits_nothing_and_opens_nothing(self, fake_pa):
        with pytest.raises(ValueError):
            play_record(a_stimulus(), **self._with_loopback())
        assert not fake_pa.written
        assert not fake_pa.opened

    def test_the_same_loopback_is_fine_on_one_device(self, monkeypatch):
        # Vacuity check: the refusal is about the split clock, not loopbacks.
        pa = FakePortAudio()
        pa.playrec = lambda playback, **kw: np.zeros(
            (playback.shape[0], 2), dtype=np.float32
        )
        monkeypatch.setitem(sys.modules, "sounddevice", pa)
        out = play_record(
            a_stimulus(),
            output_channel=0,
            input_channels=[0],
            sample_rate_hz=48_000,
            device="one device",
            loopback=LoopbackConfig(output_channel=1, input_channel=1),
        )
        assert out.shape[1] == 2


class TestTheSplitCapture:
    def _run(self, n: int = 4096, tail_s: float = 0.0) -> np.ndarray:
        return play_record(
            a_stimulus(n),
            output_channel=0,
            input_channels=[0],
            sample_rate_hz=48_000,
            device=SPLIT,
            tail_s=tail_s,
        )

    def test_it_returns_exactly_as_many_frames_as_it_played(self, fake_pa):
        # The contract every caller already relies on. The capture runs longer
        # at both ends; the window is trimmed back to the playback.
        assert self._run(n=4096).shape == (4096, 1)

    def test_the_capture_stream_starts_before_the_output(self, fake_pa):
        # A capture that starts second clips the head of the stimulus, and a
        # clipped sweep deconvolves into a smooth, plausible, wrong curve.
        self._run()
        kinds = [s.kind for s in fake_pa.opened]
        assert kinds.index("input") < kinds.index("output")

    def test_the_window_skips_what_was_captured_before_playback(self, fake_pa):
        fake_pa.input_frames_before_playback = 512
        out = self._run()
        assert out[0, 0] != 1.0, "returned the lead-in instead of the playback"

    def test_exclusive_mode_reaches_the_output_stream_only(self, fake_pa):
        self._run()
        by_kind = {s.kind: s for s in fake_pa.opened}
        assert isinstance(
            by_kind["output"].extra_settings, FakePortAudio.WasapiSettings
        )
        assert by_kind["input"].extra_settings is None

    def test_both_streams_run_at_the_one_rate_asked_for(self, fake_pa):
        # Two clocks, but one nominal rate. If these ever differ the capture
        # needs resampling before it can be deconvolved against the stimulus,
        # and nothing downstream does that.
        self._run()
        assert {s.samplerate for s in fake_pa.opened} == {48_000}

    def test_a_capture_that_delivered_nothing_raises(self, fake_pa):
        # Deconvolving an empty capture yields a smooth, plausible curve, so
        # the empty case must be an error and never an empty array.
        fake_pa.silent = True
        with pytest.raises(SafetyViolation, match="no frames"):
            self._run()

    def test_the_tail_the_caller_asked_for_comes_back(self, fake_pa):
        assert self._run(n=4096, tail_s=0.5).shape[0] == 4096 + 24_000


class TestExclusiveModeGuard:
    def test_exclusive_mode_on_a_non_wasapi_device_is_refused(self, monkeypatch):
        # Exclusive mode is a WASAPI concept. Asking for it on the MME
        # instance of the same hardware would silently not happen, and MME is
        # measured at 2-5x the scatter -- so it must not pass quietly.
        pa = FakePortAudio()
        pa.devices = {"mme thing": {"name": "mme thing", "hostapi": 1}}
        monkeypatch.setitem(sys.modules, "sounddevice", pa)
        monkeypatch.setattr("tuner.audio.io.time.sleep", lambda _s: None)
        with pytest.raises(ValueError, match="WASAPI"):
            play_record(
                a_stimulus(),
                output_channel=0,
                input_channels=[0],
                sample_rate_hz=48_000,
                device=SplitDevices(
                    output="mme thing", input="in", output_exclusive=True
                ),
            )


class TestTheCaptureOutlivesThePlayback:
    """``write()`` returns when frames are buffered, not when they are heard.

    Observed on the bench 2026-08-13 with REW also holding the device: a
    0.45 s ramp probe returned from ``write()`` almost immediately -- an
    exclusive-mode buffer swallowed the whole stimulus -- and the capture was
    then closed after ``latency + 0.05``, leaving 0.07 s of recording behind
    a 0.45 s playback. The window ran off the end and the shortfall guard
    fired.

    ``latency`` does not report how much a stream will buffer, so the wait
    cannot be derived from it. It has to be anchored to the stimulus.
    """

    def _run(self, fake_pa, n: int) -> None:
        play_record(
            a_stimulus(n),
            output_channel=0,
            input_channels=[0],
            sample_rate_hz=48_000,
            device=SPLIT,
            tail_s=0.0,
        )

    def test_it_waits_at_least_the_stimulus_duration(self, fake_pa):
        # 96000 frames at 48 kHz is 2 s. A write() that returns instantly
        # must not shorten the capture below that.
        self._run(fake_pa, 96_000)
        assert max(fake_pa.slept) >= 2.0

    def test_the_wait_scales_with_the_stimulus(self, fake_pa):
        self._run(fake_pa, 24_000)
        short = max(fake_pa.slept)
        fake_pa.slept.clear()
        self._run(fake_pa, 96_000)
        assert max(fake_pa.slept) - short == pytest.approx(1.5, abs=0.2)

    def test_the_lead_in_still_happens_before_playback(self, fake_pa):
        # The other end of the same contract: the capture starts early too.
        self._run(fake_pa, 24_000)
        assert SPLIT_LEAD_IN_S in fake_pa.slept


class TestTheBufferMatchesTheDeviceFormat:
    """Found on the bench 2026-08-13, and audible only as a wrong answer.

    A WASAPI stream in exclusive mode does no format conversion -- that is
    what exclusive mode means, and it is why this project uses it to reach
    48 kHz. Given a buffer narrower than the device's native format, the
    driver reads the samples as interleaved frames of the format it has: on a
    two-channel device a mono buffer is consumed two samples per frame, so
    every stimulus plays at **double speed, one octave high**.

    Measured, 1000 Hz through this path::

        buffer channels=1  ->  heard 1984.2 Hz
        buffer channels=2  ->  heard 1000.0 Hz

    Nothing else in the rig reports it. The wall-clock rates are right, the
    level is right, no frames are dropped, and the capture is a clean steady
    tone at the wrong frequency -- which deconvolves into a smooth, plausible
    and entirely wrong response.
    """

    def _run(self, fake_pa, output_channel: int = 0) -> None:
        play_record(
            a_stimulus(),
            output_channel=output_channel,
            input_channels=[0],
            sample_rate_hz=48_000,
            device=SPLIT,
            tail_s=0.0,
        )

    def _stream(self, fake_pa, kind: str):
        return next(s for s in fake_pa.opened if s.kind == kind)

    def test_the_output_opens_at_the_device_width_not_the_buffer_width(
        self, fake_pa
    ):
        # output_channel=0 makes a 1-column buffer, and the device has 2.
        self._run(fake_pa)
        assert self._stream(fake_pa, "output").channels == 2

    def test_the_written_buffer_is_that_wide_too(self, fake_pa):
        # The stream's channel count and the buffer's width must agree; it is
        # their disagreement that the driver reinterprets.
        self._run(fake_pa)
        assert fake_pa.written[0].shape[1] == 2

    def test_the_stimulus_stays_in_the_channel_it_was_asked_for(self, fake_pa):
        # Widening must pad, never shift. A stimulus that moved to another
        # column would measure the wrong driver, silently.
        self._run(fake_pa, output_channel=0)
        written = fake_pa.written[0]
        assert np.any(written[:, 0] != 0.0)
        assert np.all(written[:, 1] == 0.0)

    def test_a_wider_device_is_honoured(self, fake_pa):
        # An 8-out interface must open all 8, for the same reason.
        fake_pa.device_output_channels = 8
        self._run(fake_pa)
        assert self._stream(fake_pa, "output").channels == 8
        assert fake_pa.written[0].shape[1] == 8

    def test_a_buffer_wider_than_the_device_is_left_alone(self, fake_pa):
        # Vacuity: widening is a floor, not a resize. Asking for output
        # channel 3 on a 2-channel device is a caller error, and quietly
        # narrowing the buffer would hide it.
        fake_pa.device_output_channels = 2
        self._run(fake_pa, output_channel=3)
        assert fake_pa.written[0].shape[1] == 4

    def test_the_input_opens_at_the_device_width(self, fake_pa):
        # The same trap, and it bites the moment input_exclusive is set.
        self._run(fake_pa)
        assert self._stream(fake_pa, "input").channels == 2

    def test_the_requested_capture_channel_still_comes_back(self, fake_pa):
        # Opening the input wider must not change what the caller receives.
        out = play_record(
            a_stimulus(),
            output_channel=0,
            input_channels=[0],
            sample_rate_hz=48_000,
            device=SPLIT,
            tail_s=0.0,
        )
        assert out.shape == (4096, 1)


class TestTheWindowLandingPastTheRecording:
    """Found on the bench 2026-08-13, first time this path met hardware.

    A cold WASAPI exclusive open can take a long time, and ``started_at`` is
    read before ``write()`` returns. When the open is slow the capture window
    lands past the end of what was recorded, and the original code **padded
    with zeros** -- so the call returned an all-zero buffer and said nothing.

    An all-zero capture deconvolves into a smooth, entirely plausible
    frequency response. That is precisely the failure the rig-verification
    rules exist to prevent, reintroduced by the convenience of padding.
    """

    def _run(self, fake_pa):
        return play_record(
            a_stimulus(8192),
            output_channel=0,
            input_channels=[0],
            sample_rate_hz=48_000,
            device=SPLIT,
            tail_s=0.0,
        )

    def test_a_large_shortfall_raises_rather_than_padding(self, fake_pa):
        # A slow output start: plenty captured before playback began, and
        # little after it, so the window runs off the end of the recording.
        fake_pa.frames_during_playback = 1_000
        with pytest.raises(SafetyViolation, match="past the end of the recording"):
            self._run(fake_pa)

    def test_the_refusal_says_how_much_is_missing(self, fake_pa):
        fake_pa.frames_during_playback = 1_000
        with pytest.raises(SafetyViolation) as excinfo:
            self._run(fake_pa)
        text = str(excinfo.value)
        assert "frames missing" in text
        assert "smooth and completely wrong curve" in text

    def test_an_ordinary_short_tail_is_still_padded(self, fake_pa):
        # One buffer's shortfall is the normal case -- the capture closes a
        # moment after the last frame plays -- and must not become an error.
        fake_pa.frames_during_playback = 8192 - 480
        assert self._run(fake_pa).shape == (8192, 1)

    def test_a_normal_capture_is_unaffected(self, fake_pa):
        # Vacuity: the guard must not fire on the case it was built around.
        fake_pa.input_frames_before_playback = 512
        assert self._run(fake_pa).shape == (8192, 1)
