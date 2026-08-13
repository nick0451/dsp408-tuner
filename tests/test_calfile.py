"""Tests for REW-format microphone calibration files.

The interesting cases are the boundary ones: degrees-to-radians conversion at
the file boundary, and clamping rather than extrapolating outside the measured
range.
"""

import numpy as np
import pytest

from tuner.cal import CalibrationCurve, file_sha256, load, save

SAMPLE = """\
* Calibration data for mic 12345
* Freq(Hz) SPL(dB)
20.0    -2.50
100.0   -1.00
1000.0   0.00
10000.0  1.50
20000.0  3.00
"""

SAMPLE_WITH_PHASE = """\
* Freq(Hz) SPL(dB) Phase(deg)
100.0   -1.00   -18.0
1000.0   0.00     0.0
10000.0  1.50    90.0
"""


@pytest.fixture
def cal_path(tmp_path):
    p = tmp_path / "mic.cal"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


class TestLoad:
    def test_reads_frequencies_and_sensitivities(self, cal_path):
        curve = load(cal_path)
        assert curve.freqs_hz[0] == 20.0
        assert curve.sensitivity_db[-1] == 3.0
        assert curve.freqs_hz.size == 5

    def test_skips_comments(self, cal_path):
        assert load(cal_path).freqs_hz.size == 5

    def test_records_provenance(self, cal_path):
        curve = load(cal_path)
        assert curve.source == cal_path
        assert curve.sha256 == file_sha256(cal_path)

    def test_no_phase_column_yields_none(self, cal_path):
        assert load(cal_path).phase_rad is None

    def test_phase_is_converted_from_degrees(self, tmp_path):
        # The file format is degrees; everything internal is radians.
        p = tmp_path / "phase.cal"
        p.write_text(SAMPLE_WITH_PHASE, encoding="utf-8")
        curve = load(p)
        assert curve.phase_rad is not None
        assert curve.phase_rad[0] == pytest.approx(np.deg2rad(-18.0))
        assert curve.phase_rad[2] == pytest.approx(np.pi / 2)

    def test_handles_utf8_bom(self, tmp_path):
        p = tmp_path / "bom.cal"
        p.write_bytes(b"\xef\xbb\xbf" + SAMPLE.encode("utf-8"))
        assert load(p).freqs_hz.size == 5

    def test_rejects_file_with_no_data(self, tmp_path):
        p = tmp_path / "empty.cal"
        p.write_text("* only a comment\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no data rows"):
            load(p)

    def test_rejects_unparseable_row_with_line_number(self, tmp_path):
        p = tmp_path / "bad.cal"
        p.write_text("100.0 -1.0\n200.0 banana\n", encoding="utf-8")
        with pytest.raises(ValueError, match=":2:"):
            load(p)

    def test_rejects_partial_phase_column(self, tmp_path):
        p = tmp_path / "partial.cal"
        p.write_text("100.0 -1.0 5.0\n200.0 -2.0\n", encoding="utf-8")
        with pytest.raises(ValueError, match="only some rows"):
            load(p)


class TestInterpolation:
    def test_exact_points_are_returned_unchanged(self, cal_path):
        curve = load(cal_path)
        assert curve.at(np.array([1000.0]))[0] == pytest.approx(0.0)
        assert curve.at(np.array([100.0]))[0] == pytest.approx(-1.0)

    def test_interpolates_linearly_in_log_frequency(self, cal_path):
        # Geometric midpoint of 100 and 10000 is 1000, where the curve is 0.0;
        # the midpoint of the -1.0 and 1.5 endpoints in log space is 0.0 only
        # because 1000 is itself a knot. Check a true intermediate instead.
        curve = load(cal_path)
        # Halfway in log10 between 1000 (0.0 dB) and 10000 (1.5 dB).
        value = curve.at(np.array([10**3.5]))[0]
        assert value == pytest.approx(0.75, abs=1e-9)

    def test_clamps_below_range_rather_than_extrapolating(self, cal_path):
        # Extrapolating invents correction where none was measured, and does
        # so hardest at the extremes where mics behave worst.
        curve = load(cal_path)
        assert curve.at(np.array([5.0]))[0] == pytest.approx(-2.50)

    def test_clamps_above_range(self, cal_path):
        curve = load(cal_path)
        assert curve.at(np.array([30_000.0]))[0] == pytest.approx(3.00)

    def test_rejects_non_positive_frequencies(self, cal_path):
        with pytest.raises(ValueError, match="positive"):
            load(cal_path).at(np.array([0.0]))

    def test_apply_adds_correction(self, cal_path):
        curve = load(cal_path)
        freqs = np.array([100.0, 1000.0])
        corrected = curve.apply(np.array([50.0, 50.0]), freqs)
        assert corrected == pytest.approx([49.0, 50.0])

    def test_range_reports_coverage(self, cal_path):
        assert load(cal_path).range_hz == (20.0, 20_000.0)


class TestRoundTrip:
    def test_save_then_load_preserves_values(self, tmp_path, cal_path):
        original = load(cal_path)
        out = tmp_path / "out.cal"
        save(original, out)
        reloaded = load(out)
        assert np.allclose(reloaded.freqs_hz, original.freqs_hz)
        assert np.allclose(reloaded.sensitivity_db, original.sensitivity_db)

    def test_phase_survives_the_degree_round_trip(self, tmp_path):
        src = tmp_path / "phase.cal"
        src.write_text(SAMPLE_WITH_PHASE, encoding="utf-8")
        original = load(src)
        out = tmp_path / "out.cal"
        save(original, out)
        reloaded = load(out)
        assert reloaded.phase_rad is not None
        assert np.allclose(reloaded.phase_rad, original.phase_rad, atol=1e-6)

    def test_comment_is_written(self, tmp_path, cal_path):
        out = tmp_path / "out.cal"
        save(load(cal_path), out, comment="derived by substitution")
        assert "* derived by substitution" in out.read_text(encoding="utf-8")


class TestValidation:
    def test_rejects_descending_frequencies(self):
        with pytest.raises(ValueError, match="ascending"):
            CalibrationCurve(
                freqs_hz=np.array([1000.0, 100.0]),
                sensitivity_db=np.array([0.0, 1.0]),
            )

    def test_rejects_length_mismatch(self):
        with pytest.raises(ValueError, match="same length"):
            CalibrationCurve(
                freqs_hz=np.array([100.0, 1000.0]),
                sensitivity_db=np.array([0.0]),
            )

    def test_rejects_empty_curve(self):
        with pytest.raises(ValueError, match="empty"):
            CalibrationCurve(freqs_hz=np.array([]), sensitivity_db=np.array([]))

    def test_hash_changes_when_file_is_edited(self, tmp_path):
        p = tmp_path / "m.cal"
        p.write_text(SAMPLE, encoding="utf-8")
        before = file_sha256(p)
        p.write_text(SAMPLE + "21000.0 3.10\n", encoding="utf-8")
        assert file_sha256(p) != before
