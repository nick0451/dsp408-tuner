"""Reading a REW measurement export.

Fixtures are written inline rather than pointed at the bench captures: the
parser must be testable with no data files, and the real exports are 1.6 MB
each. One test does read a real export, because a parser validated only
against its author's idea of the format is a parser validated against nothing.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tuner.measure.rewfile import RewFileError, RewMeasurement, load, spread

REAL_EXPORTS = Path(__file__).resolve().parents[1] / "REW Bench Captures"

HEADER = """* Measurement data measured by REW V5.31.3
* Source: EXCL: Microphone (Umik-1  Gain: 18dB), MICROPHONE (Master), R, vol: 0.540
* Format: 256k Log Swept Sine, 1 sweep at -12.0 dBFS with no timing reference
* Dated: Aug 13, 2026 7:07:24 PM
* REW Settings:
*  C-weighting compensation: Off
*  Target level: 75.0 dB
* Note: ;
* Measurement: Bench Mic 1
* Smoothing: None
* Frequency Step: 0.36621094 Hz
* Start Frequency: 0.36621094 Hz
*
* Freq(Hz) SPL(dB) Phase(degrees)
"""

ROWS = (
    "100.000000 90.000 10.0000\n"
    "200.000000 86.000 -20.0000\n"
    "400.000000 92.000 45.0000\n"
)


def write(tmp_path: Path, body: str, name: str = "m.txt") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class TestTheHappyPath:
    @pytest.fixture
    def m(self, tmp_path) -> RewMeasurement:
        return load(write(tmp_path, HEADER + ROWS))

    def test_it_reads_the_columns(self, m):
        assert np.allclose(m.freqs_hz, [100.0, 200.0, 400.0])
        assert np.allclose(m.magnitude_dbspl, [90.0, 86.0, 92.0])

    def test_it_reads_the_header_fields_provenance_needs(self, m):
        assert m.title == "Bench Mic 1"
        assert m.measured_at == "Aug 13, 2026 7:07:24 PM"
        assert m.smoothing == "None"
        assert "-12.0 dBFS" in m.stimulus
        assert "Umik-1" in m.source

    def test_it_hashes_the_file(self, m):
        assert m.sha256 and len(m.sha256) == 64

    def test_a_different_file_hashes_differently(self, tmp_path):
        # The point of the hash: an edited export must not pass as the same
        # measurement, the same rule as the calibration file's.
        a = load(write(tmp_path, HEADER + ROWS, "a.txt"))
        b = load(write(tmp_path, HEADER + ROWS.replace("90.000", "90.001"), "b.txt"))
        assert a.sha256 != b.sha256


class TestPhaseStaysInDegreesUntilAsked:
    """The project's units rule, enforced at the file boundary."""

    def test_the_stored_column_is_degrees(self, tmp_path):
        m = load(write(tmp_path, HEADER + ROWS))
        assert np.allclose(m.phase_deg, [10.0, -20.0, 45.0])

    def test_the_accessor_converts(self, tmp_path):
        m = load(write(tmp_path, HEADER + ROWS))
        assert np.allclose(m.phase_rad, np.deg2rad([10.0, -20.0, 45.0]))

    def test_a_two_column_export_has_no_phase(self, tmp_path):
        body = HEADER + "100.0 90.0\n200.0 86.0\n"
        m = load(write(tmp_path, body))
        assert m.phase_deg is None
        assert m.phase_rad is None

    def test_phase_vanishing_partway_is_refused(self, tmp_path):
        # Rather than silently keeping a short column and misaligning it.
        body = HEADER + "100.0 90.0 1.0\n200.0 86.0\n"
        with pytest.raises(RewFileError, match="disappears"):
            load(write(tmp_path, body))


class TestInterpolation:
    @pytest.fixture
    def m(self, tmp_path) -> RewMeasurement:
        return load(write(tmp_path, HEADER + ROWS))

    def test_it_returns_the_points_it_was_given(self, m):
        assert np.allclose(m.at(np.array([100.0, 200.0, 400.0])), [90.0, 86.0, 92.0])

    def test_it_interpolates_against_log_frequency(self, m):
        # 141.42 Hz is the geometric midpoint of 100 and 200, so a log
        # interpolation lands exactly halfway between 90 and 86 dB. A linear
        # interpolation would give 88.65.
        assert m.at(np.array([np.sqrt(100.0 * 200.0)]))[0] == pytest.approx(88.0)

    def test_it_clamps_rather_than_extrapolating(self, m):
        # Extrapolating a response invents slope where nothing was measured,
        # and does it hardest at the extremes where rigs behave worst.
        assert m.at(np.array([10.0]))[0] == pytest.approx(90.0)
        assert m.at(np.array([40_000.0]))[0] == pytest.approx(92.0)

    def test_a_non_positive_frequency_is_refused(self, m):
        with pytest.raises(ValueError, match="positive"):
            m.at(np.array([0.0]))


class TestSmoothingIsVisible:
    """Fitting a smoothed curve under-corrects; the risk is not noticing."""

    def test_none_is_not_smoothed(self, tmp_path):
        assert not load(write(tmp_path, HEADER + ROWS)).is_smoothed

    def test_a_smoothed_export_says_so(self, tmp_path):
        body = HEADER.replace("* Smoothing: None", "* Smoothing: 1/3 octave")
        assert load(write(tmp_path, body + ROWS)).is_smoothed

    def test_an_unrecorded_smoothing_is_not_treated_as_smoothed(self, tmp_path):
        # Absent means an older export, not a smoothed one. Guessing the
        # stricter reading here would refuse valid files for no gain.
        body = HEADER.replace("* Smoothing: None\n", "")
        assert not load(write(tmp_path, body + ROWS)).is_smoothed


class TestRefusals:
    def test_a_header_with_no_data_is_refused(self, tmp_path):
        with pytest.raises(RewFileError, match="no data rows"):
            load(write(tmp_path, HEADER))

    def test_a_one_column_row_is_refused(self, tmp_path):
        with pytest.raises(RewFileError, match="at least two columns"):
            load(write(tmp_path, HEADER + "100.0\n200.0 86.0\n"))

    def test_a_non_numeric_row_is_refused(self, tmp_path):
        with pytest.raises(RewFileError, match="not numeric"):
            load(write(tmp_path, HEADER + "100.0 ninety\n200.0 86.0\n"))

    def test_the_error_names_the_file_and_line(self, tmp_path):
        with pytest.raises(RewFileError) as exc:
            load(write(tmp_path, HEADER + "100.0 90.0\n200.0 oops\n", "bad.txt"))
        assert "bad.txt" in str(exc.value)
        assert "line 16" in str(exc.value)

    def test_descending_frequencies_are_refused(self):
        with pytest.raises(RewFileError, match="ascending"):
            RewMeasurement(
                freqs_hz=np.array([200.0, 100.0]),
                magnitude_dbspl=np.array([1.0, 2.0]),
            )

    def test_a_single_point_is_refused(self):
        with pytest.raises(RewFileError, match="at least two points"):
            RewMeasurement(
                freqs_hz=np.array([100.0]), magnitude_dbspl=np.array([1.0])
            )


class TestSpread:
    def test_it_measures_disagreement_per_frequency(self, tmp_path):
        a = load(write(tmp_path, HEADER + ROWS, "a.txt"))
        b = load(write(tmp_path, HEADER + ROWS.replace("86.000", "88.000"), "b.txt"))
        got = spread([a, b], np.array([100.0, 200.0, 400.0]))
        assert np.allclose(got, [0.0, 2.0, 0.0])

    def test_one_measurement_cannot_have_a_spread(self, tmp_path):
        a = load(write(tmp_path, HEADER + ROWS))
        with pytest.raises(ValueError, match="at least two"):
            spread([a], np.array([100.0]))


@pytest.mark.skipif(
    not (REAL_EXPORTS / "Bench Mic 1.txt").exists(),
    reason="bench REW exports not present",
)
class TestAgainstARealExport:
    """A parser checked only against its author's idea of the format is not
    checked. These are the files REW actually wrote on 2026-08-13."""

    def test_it_reads_the_real_thing(self):
        m = load(REAL_EXPORTS / "Bench Mic 1.txt")
        assert m.title == "Bench Mic 1"
        assert m.smoothing == "None"
        assert m.freqs_hz.size > 50_000
        assert m.phase_deg is not None

    def test_the_range_covers_the_audible_band(self):
        lo, hi = load(REAL_EXPORTS / "Bench Mic 1.txt").range_hz
        assert lo < 20.0
        assert hi > 19_000.0

    def test_it_reproduces_the_finding_that_moved_the_microphone(self):
        # The far position's median spread over OUT1's passband was 3.90 dB
        # and the near-field position's 1.53 dB. That comparison is why the
        # microphone moved, so it is worth pinning.
        axis = np.geomspace(450.0, 3500.0, 300)
        far = [load(REAL_EXPORTS / f"Bench Mic {i}.txt") for i in range(1, 6)]
        assert np.median(spread(far, axis)) == pytest.approx(3.90, abs=0.05)

        near_names = ["20 cm test.txt"] + [f"L 20 cm test {i}.txt" for i in range(1, 5)]
        if not all((REAL_EXPORTS / n).exists() for n in near_names):
            pytest.skip("near-field set not present")
        near = [load(REAL_EXPORTS / n) for n in near_names]
        assert np.median(spread(near, axis)) == pytest.approx(1.53, abs=0.05)
