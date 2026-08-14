"""The REW REST client, against a fake that behaves like the real API did.

The fake is modelled on **observed** behaviour of REW 5.40 beta 132, API
0.9.6, not on the documentation -- including the two ways it misleads a
caller: an import is asynchronous, and a payload it cannot decode is accepted
with 202 and then silently dropped.
"""

from __future__ import annotations

import base64
import json

import numpy as np
import pytest

from tuner.measure.rewapi import RewApi, RewApiError, log_axis


class FakeRew:
    """Enough of REW's API to exercise the client, including its traps."""

    def __init__(self, *, drop_imports=False, import_delay=0):
        self.measurements: dict[str, dict] = {}
        self.responses: dict[str, tuple[float, int, np.ndarray]] = {}
        self.calls: list[tuple[str, str]] = []
        self.drop_imports = drop_imports
        self.import_delay = import_delay
        self._pending: list[tuple[str, dict]] = []
        self._next = 1

    def seed(self, title: str, start: float, ppo: int, magnitude: np.ndarray) -> str:
        index = str(self._next)
        self._next += 1
        self.measurements[index] = {"title": title, "uuid": f"uuid-{index}"}
        self.responses[index] = (start, ppo, np.asarray(magnitude, dtype=np.float64))
        return index

    def __call__(self, method: str, path: str, payload=None):
        self.calls.append((method, path))

        # Deliver any import that has waited long enough. Models REW's 202:
        # the measurement appears some polls after the POST returns.
        if self._pending and method == "GET" and path == "/measurements":
            still: list[tuple[str, dict]] = []
            for remaining, body in self._pending:
                if remaining <= 0:
                    self.seed(
                        body["identifier"],
                        body["startFreq"],
                        body["ppo"],
                        np.frombuffer(base64.b64decode(body["magnitude"]), dtype=">f4"),
                    )
                else:
                    still.append((remaining - 1, body))
            self._pending = still

        if method == "GET" and path == "/version":
            return 200, json.dumps({"message": "5.40 Beta 132 API 0.9.6"})
        if method == "GET" and path == "/measurements":
            return 200, json.dumps(self.measurements)
        if method == "POST" and path == "/import/frequency-response-data":
            if not self.drop_imports:
                self._pending.append((self.import_delay, payload))
            # 202 either way. That is the trap.
            return 202, json.dumps({"message": "in progress"})
        if "/frequency-response" in path or "/target-response" in path:
            index = path.split("/")[2]
            if index not in self.responses:
                return 404, json.dumps({"message": "no such measurement"})
            start, ppo, magnitude = self.responses[index]
            smoothing = path.split("smoothing=")[1] if "smoothing=" in path else "None"
            return 200, json.dumps(
                {
                    "unit": "SPL",
                    "smoothing": smoothing,
                    "startFreq": start,
                    "ppo": ppo,
                    "magnitude": base64.b64encode(
                        magnitude.astype(">f4").tobytes()
                    ).decode(),
                }
            )
        return 404, json.dumps({"message": f"no route for {path}"})


@pytest.fixture
def rew():
    fake = FakeRew()
    return RewApi(transport=fake), fake


class TestReading:
    def test_version(self, rew):
        api, _ = rew
        assert "API 0.9.6" in api.version()

    def test_measurements_are_keyed_by_index(self, rew):
        api, fake = rew
        fake.seed("first", 20.0, 48, np.zeros(10))
        assert api.measurements()["1"]["title"] == "first"

    def test_a_response_comes_back_on_a_reconstructed_log_axis(self, rew):
        # The import sends no frequency column, so the axis is implied by
        # startFreq and ppo. Getting that reconstruction wrong shifts every
        # curve silently, which is why it is asserted rather than assumed.
        api, fake = rew
        fake.seed("m", 20.0, 48, np.arange(97, dtype=float))
        got = api.frequency_response(1)
        assert got.freqs_hz[0] == pytest.approx(20.0)
        assert got.freqs_hz[48] == pytest.approx(40.0)
        assert got.freqs_hz[96] == pytest.approx(80.0)

    def test_magnitudes_survive_the_round_trip(self, rew):
        api, fake = rew
        magnitude = np.linspace(-30.0, 10.0, 64)
        fake.seed("m", 20.0, 24, magnitude)
        assert np.allclose(api.frequency_response(1).magnitude_dbspl, magnitude,
                           atol=1e-4)

    def test_smoothing_is_passed_to_rew(self, rew):
        # Applied by REW rather than by us, so what we fit is what the
        # operator looked at on screen.
        api, fake = rew
        fake.seed("m", 20.0, 24, np.zeros(64))
        assert api.frequency_response(1, smoothing="1/6").smoothing == "1/6"
        assert any("smoothing=1/6" in path for _, path in fake.calls)

    def test_an_unknown_smoothing_is_refused_before_the_call(self, rew):
        api, fake = rew
        with pytest.raises(ValueError, match="not one of"):
            api.frequency_response(1, smoothing="1/7")
        assert not fake.calls

    def test_a_missing_measurement_is_an_error(self, rew):
        api, _ = rew
        with pytest.raises(RewApiError, match="HTTP 404"):
            api.frequency_response(99)


class TestBothAxisConventions:
    """REW describes an imported response and a measured one differently.

    An import comes back log-spaced (``ppo``) because that is how it was
    sent; a sweep REW ran itself comes back linearly spaced (``freqStep``)
    because that is what an FFT produces. The client was first written
    against imports alone and fell over on the first real measurement.
    """

    def _api(self, payload):
        def transport(method, path, _payload=None):
            return 200, json.dumps(payload)

        return RewApi(transport=transport)

    def _payload(self, magnitude, **extra):
        return {
            "startFreq": 0.0 if "freqStep" in extra else 20.0,
            "magnitude": base64.b64encode(
                np.asarray(magnitude, dtype=">f4").tobytes()
            ).decode(),
            **extra,
        }

    def test_a_log_axis_uses_ppo(self):
        got = self._api(self._payload(np.zeros(49), ppo=48)).frequency_response(1)
        assert got.freqs_hz[0] == pytest.approx(20.0)
        assert got.freqs_hz[48] == pytest.approx(40.0)

    def test_a_linear_axis_uses_freqstep(self):
        got = self._api(self._payload(np.zeros(5), freqStep=10.0)).frequency_response(1)
        # DC is dropped, so the first surviving point is 10 Hz.
        assert np.allclose(got.freqs_hz, [10.0, 20.0, 30.0, 40.0])

    def test_dc_is_dropped_with_its_magnitude(self):
        # Dropping the frequency and keeping the value would misalign every
        # point after it -- worse than either keeping or refusing.
        got = self._api(
            self._payload([1.0, 2.0, 3.0, 4.0], freqStep=10.0)
        ).frequency_response(1)
        assert np.allclose(got.magnitude_dbspl, [2.0, 3.0, 4.0])

    def test_neither_convention_is_an_error(self):
        api = self._api({"startFreq": 20.0, "magnitude": base64.b64encode(
            np.zeros(4, dtype=">f4").tobytes()).decode()})
        with pytest.raises(RewApiError, match="neither a log axis"):
            api.frequency_response(1)


class TestImportIsVerifiedNotAssumed:
    """The trap: 202 does not mean the import happened."""

    def test_a_good_import_returns_the_new_index(self, rew):
        api, _ = rew
        index = api.import_response("ours", np.zeros(32), 20.0, 24)
        assert index == "1"

    def test_the_imported_curve_reads_back(self, rew):
        api, _ = rew
        magnitude = np.linspace(-12.0, 3.0, 48)
        index = api.import_response("ours", magnitude, 20.0, 24)
        assert np.allclose(
            api.frequency_response(index).magnitude_dbspl, magnitude, atol=1e-4
        )

    def test_an_import_that_never_lands_raises(self):
        # Observed for real: a little-endian payload was accepted with 202
        # and never appeared. Returning on the status code would have
        # reported success for an import that did not happen.
        api = RewApi(transport=FakeRew(drop_imports=True))
        with pytest.raises(RewApiError, match="never appeared"):
            api.import_response("ours", np.zeros(32), 20.0, 24, settle_s=0.5)

    def test_it_waits_for_an_import_that_is_merely_slow(self):
        # The other half: 202 is asynchronous even when it works, so the
        # client must poll rather than check once.
        api = RewApi(transport=FakeRew(import_delay=3))
        assert api.import_response("ours", np.zeros(32), 20.0, 24, settle_s=5.0) == "1"

    def test_non_finite_magnitudes_are_refused(self, rew):
        api, fake = rew
        bad = np.array([1.0, np.nan, 2.0, 3.0])
        with pytest.raises(ValueError, match="non-finite"):
            api.import_response("ours", bad, 20.0, 24)
        assert not fake.calls

    def test_a_degenerate_curve_is_refused(self, rew):
        api, _ = rew
        with pytest.raises(ValueError, match="at least 2 points"):
            api.import_response("ours", np.array([1.0]), 20.0, 24)

    def test_the_payload_is_big_endian(self, rew):
        # Little-endian is the failure REW does not report. Pinned here
        # because nothing downstream would catch a regression.
        sent: list[dict] = []
        fake = FakeRew()
        original = fake.__call__

        def spy(method, path, payload=None):
            if path == "/import/frequency-response-data":
                sent.append(payload)
            return original(method, path, payload)

        api = RewApi(transport=spy)
        magnitude = np.array([-6.0, 0.0, 6.0, 12.0])
        api.import_response("ours", magnitude, 20.0, 24)
        raw = base64.b64decode(sent[0]["magnitude"])
        assert np.allclose(np.frombuffer(raw, dtype=">f4"), magnitude)


class TestAutomatedMeasurement:
    """``POST /measure/command`` -- the one Pro-gated call, and the one that
    emits sound outside ``tuner.safety``. Confirmed on hardware 2026-08-13."""

    def test_it_returns_the_new_measurement(self):
        fake = FakeRew()
        original = fake.__call__

        def with_measure(method, path, payload=None):
            if method == "POST" and path == "/measure/command":
                fake.seed("swept", 20.0, 24, np.zeros(48))
                return 202, json.dumps({"message": "Starting measurement"})
            return original(method, path, payload)

        assert RewApi(transport=with_measure).measure() == "1"

    def test_a_command_that_produces_nothing_raises(self):
        # Without Pro the command is accepted and nothing happens, which is
        # indistinguishable from success at the status code.
        def accepts_and_does_nothing(method, path, payload=None):
            if method == "POST":
                return 202, json.dumps({"message": "Starting measurement"})
            return 200, json.dumps({})

        api = RewApi(transport=accepts_and_does_nothing)
        with pytest.raises(RewApiError, match="Pro upgrade"):
            api.measure(settle_s=0.5)

    def test_the_level_is_settable(self):
        sent = []

        def spy(method, path, payload=None):
            sent.append((method, path, payload))
            return 200, json.dumps({"message": "Level set"})

        RewApi(transport=spy).set_level(-20.0)
        assert sent == [("POST", "/measure/level",
                         {"value": -20.0, "unit": "dBFS"})]


class TestTheLogAxis:
    def test_it_matches_what_the_import_implies(self):
        # log_axis exists so a caller cannot resample onto a grid a fraction
        # of a bin away from the one REW reconstructs.
        axis = log_axis(20.0, 20_000.0, 48)
        assert axis[0] == pytest.approx(20.0)
        assert np.allclose(axis, 20.0 * 2.0 ** (np.arange(axis.size) / 48))

    def test_it_never_overshoots_the_requested_stop(self):
        # A log grid at a given ppo generally cannot land on an arbitrary
        # stop: 48 ppo from 20 Hz gives 19897 Hz and then 20187 Hz. Rounding
        # to the nearer would sometimes exceed the range the caller measured,
        # and interpolation answers that with a flat continuation rather than
        # an error -- so it must truncate, and by less than one spacing.
        for stop, ppo in ((20_000.0, 48), (3500.0, 24), (19_999.0, 96)):
            axis = log_axis(20.0, stop, ppo)
            assert axis[-1] <= stop
            assert axis[-1] * 2.0 ** (1.0 / ppo) > stop

    def test_an_exact_range_lands_on_its_endpoint(self):
        # Vacuity: truncation must not cost a point when the range does
        # divide evenly.
        axis = log_axis(20.0, 40.0, 12)
        assert axis[-1] == pytest.approx(40.0)

    def test_ppo_sets_the_spacing(self):
        assert log_axis(20.0, 40.0, 12).size == 13

    def test_a_backwards_range_is_refused(self):
        with pytest.raises(ValueError, match="must exceed"):
            log_axis(1000.0, 100.0, 24)


class TestUnreachable:
    def test_the_error_says_how_to_turn_the_api_on(self):
        # The API is off by default, so this is the first thing most callers
        # will hit and the message has to be the fix, not the symptom.
        api = RewApi(base_url="http://127.0.0.1:1")
        with pytest.raises(RewApiError, match="-api"):
            api.version()
