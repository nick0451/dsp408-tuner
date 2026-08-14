"""Talk to a running Room EQ Wizard over its REST API.

Explored against **REW 5.40 beta 132, API 0.9.6** on 2026-08-13. Everything
below was measured against that build, not read off documentation.

**Why this exists, and why it is not a replacement for the measurement
engine.** REW does two things this project cannot: harmonic distortion, and
dB SPL against a calibrated microphone. It also ships target curves with a
citable source, which matters because ``optimize.target.harman_in_car``
deliberately *raises* rather than reproducing published values from memory.
What it cannot do is talk to a DSP-408, fit under that device's real
constraints, or pass a stimulus through :mod:`tuner.safety`.

**The architecture this makes possible, and it is better than the obvious
one.** ``POST /import/frequency-response-data`` accepts a curve we measured
ourselves. So the loop is:

    our ramped, level-limited sweep  ->  import into REW  ->  REW's targets,
    smoothing and analysis  ->  our fitter under the device's constraints
    ->  our RFCOMM write

Two consequences fall out, and both were the sticking points an hour before
this module existed:

* **No Pro licence is needed.** ``GET`` works on any installation and so does
  this import; only *REW running its own sweep* requires the upgrade.
* **Hard safety rule 1 is not bypassed.** The sweep that plays is ours, so it
  ramps from -30 dBFS, respects the channel's ``DriverCeiling``, and has the
  DSP's own gain subtracted from it. Handing sweep duty to REW would have
  made that rule structurally unenforceable in a car.

Four things this API does that will bite a caller who assumes otherwise:

1. **Magnitude is base64 of big-endian IEEE floats**, not text. A text list
   is rejected outright, which is the good case.
2. **A little-endian import is accepted with 202 and then silently
   dropped.** It never appears in ``/measurements``. So *an accepted POST is
   not a completed import* -- the same shape as this project's finding that
   on a DSP-408 a verified write is not a working write. :func:`import_response`
   therefore polls for the identifier instead of trusting the status code.
3. **202 means "in progress".** Even a good import is asynchronous.
4. **``DELETE /measurements/{id}`` hung** on the build tested, with the rest
   of the API still responsive. Every call here carries a timeout, and
   nothing in the tuning path depends on deleting anything.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import numpy as np

from .rewfile import RewMeasurement

#: Where REW's API listens. Localhost only -- it cannot be reached remotely,
#: which is an architecture constraint rather than a default: REW and whatever
#: drives it must be the same machine.
DEFAULT_BASE_URL = "http://127.0.0.1:4735"

#: Seconds before a call is abandoned. Generous for a local HTTP server
#: because REW does real work behind some endpoints, and finite because one
#: of them was observed not to return at all.
DEFAULT_TIMEOUT_S = 20.0

#: How long :func:`import_response` waits for an asynchronous import to show
#: up in the measurement list before calling it failed.
IMPORT_SETTLE_S = 10.0

#: Smoothing values REW accepts on a frequency-response request.
SMOOTHING_CHOICES = (
    "1/1", "1/2", "1/3", "1/6", "1/12", "1/24", "1/48", "Var", "Psy", "ERB", "None",
)


class RewApiError(RuntimeError):
    """The API refused, was unreachable, or returned something unusable."""


@dataclass
class RewApi:
    """A thin, dependency-free client. ``urllib`` only, by design.

    This project runs on a Raspberry Pi eventually and its install should not
    grow an HTTP stack for a handful of local GETs.
    """

    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = DEFAULT_TIMEOUT_S
    #: Injected in tests. Signature mirrors :meth:`_request`.
    transport: object | None = field(default=None, repr=False)

    # -- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str, payload=None):
        if self.transport is not None:
            return self.transport(method, path, payload)
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                text = response.read().decode("utf-8", "replace")
                return response.status, text
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except OSError as exc:
            raise RewApiError(
                f"cannot reach REW at {self.base_url}: {exc}.\n\n"
                f"The API is off by default. Start REW with '-api', or enable "
                f"it in Preferences. It listens on localhost only, so REW and "
                f"this program must be on the same machine."
            ) from exc

    def _json(self, method: str, path: str, payload=None):
        status, text = self._request(method, path, payload)
        if status not in (200, 202):
            raise RewApiError(f"{method} {path} -> HTTP {status}: {text[:300]}")
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RewApiError(
                f"{method} {path} returned non-JSON: {text[:200]}"
            ) from exc

    # -- reading ----------------------------------------------------------

    def version(self) -> str:
        """REW's version string, and the first call worth making.

        Doubles as the reachability check: everything else assumes the API is
        up, and a clear failure here beats a confusing one later.
        """
        return str(self._json("GET", "/version").get("message", "")).strip()

    def measurements(self) -> dict[str, dict]:
        """Every measurement REW currently holds, keyed by its 1-based index.

        The index is positional and **moves when measurements are added or
        removed**. Each entry also carries a ``uuid``, which does not; prefer
        it whenever a reference has to outlive the call that obtained it.
        """
        return self._json("GET", "/measurements") or {}

    def frequency_response(
        self, ident: str | int, smoothing: str | None = None
    ) -> RewMeasurement:
        """One measurement's magnitude response, as a :class:`RewMeasurement`.

        ``smoothing`` is applied by REW, which is worth preferring over
        smoothing the result ourselves: it is the same code path the operator
        sees on screen, so what we fit is what they looked at.

        **Smoothing is not cosmetic here.** Fitting an unsmoothed near-field
        curve against a flat target made this project's own fitter ask for
        +15.9 dB of channel gain, because a peaking chain cannot fill a 25 dB
        cancellation and the only way left to reduce rms error is to cut
        everything else down to meet it. 1/6 octave removes the narrow
        cancellations a correction should not attempt while keeping the broad
        shape it should.
        """
        if smoothing is not None and smoothing not in SMOOTHING_CHOICES:
            raise ValueError(
                f"smoothing {smoothing!r} is not one of {SMOOTHING_CHOICES}"
            )
        query = f"?smoothing={smoothing}" if smoothing else ""
        payload = self._json("GET", f"/measurements/{ident}/frequency-response{query}")
        return self._to_measurement(payload, ident, kind="frequency response")

    def target_response(self, ident: str | int) -> RewMeasurement:
        """REW's target curve for a measurement, as data.

        **The sanctioned way to obtain a published target.**
        ``optimize.target.harman_in_car`` raises rather than reproducing curve
        values from memory, because a wrong target is inherited by every tune
        afterwards and no measurement can reveal it -- the tune will faithfully
        match whatever curve it was handed. Its docstring names exactly this
        route: export the curve from software that ships it.

        Shape it by :meth:`set_target_settings` and
        :meth:`set_room_curve_settings` first; this reads back the result.
        """
        payload = self._json("GET", f"/measurements/{ident}/target-response")
        return self._to_measurement(payload, ident, kind="target response")

    def target_settings(self, ident: str | int) -> dict:
        return self._json("GET", f"/measurements/{ident}/target-settings")

    def room_curve_settings(self, ident: str | int) -> dict:
        return self._json("GET", f"/measurements/{ident}/room-curve-settings")

    def distortion(self, ident: str | int) -> dict:
        """Harmonic distortion, which this project cannot measure at all.

        The gap that motivated the whole hybrid: on 2026-08-13 the operator
        could *hear* distortion while our only instrument was an indirect
        level-linearity check fighting the noise floor of a room.
        """
        return self._json("GET", f"/measurements/{ident}/distortion")

    # -- writing ----------------------------------------------------------

    def set_target_settings(self, ident: str | int, settings: dict) -> None:
        self._json("POST", f"/measurements/{ident}/target-settings", settings)

    def set_room_curve_settings(self, ident: str | int, settings: dict) -> None:
        self._json("POST", f"/measurements/{ident}/room-curve-settings", settings)

    def output_device(self) -> str:
        """The output REW currently holds."""
        return str(self._json("GET", "/audio/java/output-device")["device"])

    def output_devices(self) -> list[str]:
        return list(self._json("GET", "/audio/java/output-devices") or [])

    def set_output_device(self, device: str) -> None:
        """Point REW at a different output, which **releases the old one**.

        **The hybrid needs this, and finding out why cost an evening.** REW
        keeps its output device claimed after measuring -- in WASAPI
        exclusive mode, so our own open then fails outright with
        ``Invalid device``. Two programs cannot share the interface, and
        nothing announces the conflict.

        The dangerous part is that the failure is not always loud. At a lower
        stimulus level the same contention produced a *quiet capture* rather
        than an error: our stream opened, the sweep played into nothing much,
        and only ``require_signal_response`` caught it. A hard error is the
        good case.

        So a hybrid loop hands the device back and forth explicitly: park REW
        on some other output while we sweep, and give it back afterwards.
        Setting the device also resets the sweep level, so re-apply
        :meth:`set_level` after returning it.
        """
        self._json("POST", "/audio/java/output-device", {"device": device})

    def set_level(self, level_dbfs: float) -> None:
        """REW's sweep level, in dBFS.

        Worth setting from ``DspBackend.stimulus_limit`` rather than leaving
        at whatever the operator last used -- but read the warning on
        :meth:`measure` before treating that as equivalent to our own limiter.
        """
        self._json("POST", "/measure/level", {"value": float(level_dbfs),
                                              "unit": "dBFS"})

    def measure(self, command: str = "SPL", settle_s: float = 120.0) -> str:
        """Have REW run a sweep, and return the new measurement's index.

        Requires the **Pro** upgrade; every other method here works on a
        plain installation. Confirmed working 2026-08-13.

        .. warning::
           **This is the one call in this module that emits sound, and it
           does not pass through** :mod:`tuner.safety`. There is no ramp from
           -30 dBFS, no per-channel ``DriverCeiling`` with a written basis,
           and no subtraction of the DSP's own gain and EQ boost -- hard
           safety rules 1, 2 and 6, none of which this project can enforce
           over a stimulus another program plays.

           Two mitigations, and they are not equivalent. :meth:`set_level`
           caps what REW *asks* for, which is enforcement by trusting a
           setting. Setting the DSP channel's own gain first is enforcement
           in hardware we control and which sits downstream of REW, which is
           strictly stronger; it inverts rule 6 from a hazard into the
           enforcement point.

           With anything fragile connected, prefer measuring with our own
           ramped sweep and :meth:`import_response`. The whole reason that
           path exists is that it keeps REW's analysis without giving up the
           limiter.

        Asynchronous like the import, and polled for the same reason: the
        202 says the sweep started, not that it finished or succeeded.
        """
        before = set(self.measurements())
        self._json("POST", "/measure/command", {"command": command})
        deadline = time.monotonic() + settle_s
        while time.monotonic() < deadline:
            time.sleep(0.5)
            fresh = set(self.measurements()) - before
            if fresh:
                return sorted(fresh, key=lambda k: int(k) if k.isdigit() else 0)[-1]
        raise RewApiError(
            f"REW accepted a {command!r} measurement but none appeared within "
            f"{settle_s:.0f} s. Automated measurement needs the Pro upgrade; "
            f"without it the command is accepted and nothing happens."
        )

    def import_response(
        self,
        identifier: str,
        magnitude_db: np.ndarray,
        start_freq_hz: float,
        ppo: int,
        settle_s: float = IMPORT_SETTLE_S,
    ) -> str:
        """Push a curve **we** measured into REW, and confirm it arrived.

        This is the endpoint the whole hybrid rests on. It means REW's
        targets, smoothing and analysis apply to a sweep that passed through
        :mod:`tuner.safety` -- ramped, ceiling-limited, with the DSP's own
        gain subtracted -- rather than one REW played itself.

        The axis is **log-spaced by construction**: point *i* sits at
        ``start_freq_hz * 2 ** (i / ppo)``. There is no frequency column to
        send, so a caller resampling onto this grid is required rather than
        encouraged; :func:`log_axis` builds it.

        **Verified by readback, not by status code.** A little-endian payload
        is accepted with 202 and then silently dropped, never appearing in
        the measurement list. Returning on the 202 would report success for
        an import that did not happen -- the same failure this project already
        documented on the DSP, where an acked, byte-identical readback of an
        EQ band said nothing about whether the firmware executed it.
        """
        magnitude_db = np.asarray(magnitude_db, dtype=np.float64)
        if magnitude_db.ndim != 1 or magnitude_db.size < 2:
            raise ValueError("magnitude_db must be a 1-D array of at least 2 points")
        if not np.all(np.isfinite(magnitude_db)):
            raise ValueError(
                "magnitude_db contains non-finite values. REW will accept "
                "them and the result is not a measurement."
            )
        if ppo < 1:
            raise ValueError("ppo must be at least 1")

        before = set(self._identifiers())
        self._json(
            "POST",
            "/import/frequency-response-data",
            {
                "identifier": identifier,
                "isImpedance": False,
                "startFreq": float(start_freq_hz),
                "ppo": int(ppo),
                # Big-endian. Java's ByteBuffer default, and the only width
                # order REW decodes -- see the module docstring.
                "magnitude": base64.b64encode(
                    magnitude_db.astype(">f4").tobytes()
                ).decode(),
            },
        )

        deadline = time.monotonic() + settle_s
        while time.monotonic() < deadline:
            for index, meta in self.measurements().items():
                if meta.get("title") == identifier and index not in before:
                    return index
            time.sleep(0.2)
        raise RewApiError(
            f"REW accepted the import of {identifier!r} but it never appeared "
            f"in the measurement list within {settle_s:.0f} s. The API returns "
            f"202 before the import is done and drops payloads it cannot "
            f"decode, so an accepted POST is not a completed import."
        )

    # -- internals --------------------------------------------------------

    def _identifiers(self) -> dict[str, str]:
        return {k: v.get("title", "") for k, v in self.measurements().items()}

    def _to_measurement(self, payload, ident, kind: str) -> RewMeasurement:
        """Both axis conventions, because REW uses both.

        An **imported** response comes back log-spaced, described by ``ppo``,
        because that is how it was sent. A response REW **measured itself**
        comes back linearly spaced, described by ``freqStep``, because that is
        what an FFT produces. The two never appear together and the client was
        first written against imports alone -- which is the same defect as
        validating a file parser against nothing but its author's idea of the
        format.
        """
        if not payload or "magnitude" not in payload:
            raise RewApiError(f"{kind} for {ident} carried no magnitude data")
        start = float(payload["startFreq"])
        magnitude = _decode_floats(base64.b64decode(payload["magnitude"]))
        index = np.arange(magnitude.size)

        ppo = payload.get("ppo")
        step = payload.get("freqStep")
        if ppo:
            freqs = start * 2.0 ** (index / int(ppo))
        elif step:
            freqs = start + index * float(step)
        else:
            raise RewApiError(
                f"{kind} for {ident} describes neither a log axis ('ppo') nor "
                f"a linear one ('freqStep'); got keys {sorted(payload)}. "
                f"Without one the frequencies are unknown and the magnitudes "
                f"are unusable."
            )

        # A linear axis from an FFT starts at DC, and a zero frequency is not
        # a point a log-interpolating consumer can use. Drop it rather than
        # let it propagate into a log10.
        if freqs[0] <= 0.0:
            keep = freqs > 0.0
            freqs, magnitude = freqs[keep], magnitude[keep]
        return RewMeasurement(
            freqs_hz=freqs,
            magnitude_dbspl=magnitude,
            title=str(ident),
            smoothing=str(payload.get("smoothing", "")),
            stimulus=f"REW API {kind}",
        )


def _decode_floats(raw: bytes) -> np.ndarray:
    """Big-endian floats, width inferred and then sanity-checked.

    REW echoes back the width it was given -- a float32 import reads back as
    float32 and a float64 one as float64 -- and the response carries no field
    saying which. Length alone is ambiguous whenever the byte count divides by
    eight, so the decode is confirmed against physical plausibility: a
    magnitude response in dB does not exceed a few hundred, while the same
    bytes read at the wrong width produce values around 1e36.
    """
    def plausible(values: np.ndarray) -> bool:
        return bool(values.size and np.all(np.isfinite(values))
                    and np.max(np.abs(values)) < 1000.0)

    for dtype in (">f4", ">f8"):
        if len(raw) % np.dtype(dtype).itemsize:
            continue
        values = np.frombuffer(raw, dtype=dtype).astype(np.float64)
        if plausible(values):
            return values
    raise RewApiError(
        f"could not decode {len(raw)} bytes of magnitude data as big-endian "
        f"float32 or float64 -- every reading produced implausible levels."
    )


def log_axis(start_freq_hz: float, stop_freq_hz: float, ppo: int) -> np.ndarray:
    """The exact grid :meth:`RewApi.import_response` will assume.

    Built here so a caller cannot resample onto an axis a fraction of a bin
    away from the one REW reconstructs. The import sends no frequency column,
    so a mismatch would shift the whole curve silently.

    **The last point never exceeds ``stop_freq_hz``.** A log grid at a given
    points-per-octave generally cannot land on an arbitrary stop frequency --
    48 ppo from 20 Hz reaches 19 897 Hz and then 20 187 Hz, with nothing in
    between. Rounding to the nearer of those would sometimes overshoot, and a
    caller resampling a measurement onto the result would then be asking for
    data above the range they measured, which interpolation supplies as a
    flat continuation rather than an error. Truncating undershoots by less
    than one point's spacing and cannot invent anything.
    """
    if stop_freq_hz <= start_freq_hz:
        raise ValueError("stop_freq_hz must exceed start_freq_hz")
    if ppo < 1:
        raise ValueError("ppo must be at least 1")
    count = int(np.floor(ppo * np.log2(stop_freq_hz / start_freq_hz))) + 1
    return start_freq_hz * 2.0 ** (np.arange(count) / ppo)
