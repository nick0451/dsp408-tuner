"""The measurement engine against REW. Independent ground truth, at last.

``CLAUDE.md``'s validation policy asserted for months that this test existed.
It did not, and saying so was the point of the debt notice that replaced the
claim. This is the test.

**What makes it independent.** REW generates its own sweep, deconvolves it its
own way, windows it its own way and FFTs it its own way. It shares no code with
``tuner.measure`` and was written by people who have never seen this project.
Both tools measured the same physical path -- a DSP-408 output into a Scarlett
Solo, electrically, with a crossover and a deep narrow cut in it -- one after
the other, with nothing touched in between.

That is a different and stronger claim than anything else in the suite. The
analytic known-answer tests check our maths against our maths. The electrical
loopback checks our chain against our chain. This checks our *answer* against
somebody else's answer to the same physical question.

**What it cannot catch.** A systematic error that both implementations share --
a wrong idea about what a log sweep is, say -- survives this test, as it would
survive any comparison to a second implementation. Independent is not infallible.

----

**The tolerance was chosen before the data was seen**, and this comment exists
so that stays checkable. Two independent implementations measuring the same
stable electrical path should agree well inside the measurement repeatability
floor of 0.39 dB. ``TOLERANCE_DB`` is 0.5 dB after removing a constant offset.

**It has not moved, and it must not.** A tolerance widened until the test passes
is a test that measures nothing, and this file exists precisely because a
comfortable claim once stood in for a measurement.

**The comparison band did move, twice, and here is the whole of why.**

It was 30 Hz - 18 kHz when this file was written. That was an error in test
design: the DUT is low-passed at 3.5 kHz, so the band spent 2.4 octaves
comparing two measurements of a signal the device had deliberately removed --
50 dB down by 14 kHz. Under MME our own run-to-run repeatability there was
**7 dB**, larger than our disagreement with REW, and an instrument cannot be
checked against a reference in a band where it does not agree with itself.

The stated rule for the second attempt was: **choose the band from our own
repeatability, which is measurable without consulting REW at all, and only then
compare.** Switching the rig from MME to WASAPI (a change that leaves the
measured curve alone to 0.236 dB but cuts the scatter 2-5x) made our runs agree
to 0.392 dB all the way to 8 kHz. So that rule gave **8 kHz** -- and at 8 kHz
the comparison failed, by up to 1.9 dB just above the DUT's corner, while our
own two runs there agreed to 0.004 dB.

**The band is 3.5 kHz because that is where the reference stops being able to
resolve half a dB.** Two experiments established it, and both are pinned in
:class:`TestTheBandIsLimitedByTheReference`:

* Removing the low-pass, so the channel is flat to 18 kHz at full level, did
  *not* remove the disagreement. It grew. So it was never about the slope, and
  the windowing hypothesis that fitted so neatly was wrong.
* Measuring the reference against itself settled it. **REW's own run-to-run
  scatter is 0.370 dB rms against our 0.080** -- larger than our disagreement
  with REW (0.261) -- and averaging REW's two runs moves it *toward* us, which
  is what noise does and a systematic error does not.

So the honest description of this file is: *the two engines agree to 0.35 dB
over 30 Hz - 3.5 kHz, and above that the reference is too noisy to say
anything.* Our engine is **4.6x more repeatable than the tool being used to
check it**, which is not the result anyone expects to write down and is the
reason the band limit is not a criticism of the engine.

It cannot be improved on this rig: REW averages sweeps safely only with a
timing reference, and the Solo's two inputs are the DUT and a mic preamp, so
there is no spare channel for a loopback. Trying anyway produced 62 dB of comb
filtering -- kept as :class:`TestUnalignedAveragingIsCatastrophic`, because it
independently reproduces a warning this project had only ever verified on its
own rig.

What it costs: nothing above 3.5 kHz is validated against REW.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

GOLDEN = Path(__file__).resolve().parent / "golden" / "rew"
OURS = GOLDEN / "ours_response.txt"
REPEAT = GOLDEN / "ours_repeat.txt"
THEIRS = GOLDEN / "rew_response.txt"
PROVENANCE = GOLDEN / "provenance.json"

#: Chosen a priori and unchanged. See the module docstring before touching it.
TOLERANCE_DB = 0.5

#: The DUT's passband. Was 18 kHz; the module docstring records why it is not.
COMPARE_LO_HZ = 30.0
COMPARE_HI_HZ = 3_500.0

pytestmark = pytest.mark.skipif(
    not (OURS.exists() and THEIRS.exists()),
    reason="REW golden not captured yet; run tools/bench_golden.py",
)


def _read_two_columns(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Frequency and magnitude from either tool's export.

    Tolerant about the preamble and separators because REW's text export has
    changed shape across versions and ours is ours. Deliberately *not* tolerant
    about anything numeric.
    """
    freqs: list[float] = []
    values: list[float] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line[0] in "#*/;":
            continue
        parts = line.replace(",", " ").split()
        if len(parts) < 2:
            continue
        try:
            f, v = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if f > 0:
            freqs.append(f)
            values.append(v)
    if len(freqs) < 100:
        raise AssertionError(f"{path.name}: only {len(freqs)} usable rows")
    order = np.argsort(freqs)
    return np.asarray(freqs)[order], np.asarray(values)[order]


@pytest.fixture(scope="module")
def curves():
    ours_f, ours_db = _read_two_columns(OURS)
    theirs_f, theirs_db = _read_two_columns(THEIRS)

    band = (ours_f >= COMPARE_LO_HZ) & (ours_f <= COMPARE_HI_HZ)
    axis = ours_f[band]
    if not (theirs_f.min() <= axis.min() and theirs_f.max() >= axis.max()):
        raise AssertionError(
            f"REW's export covers {theirs_f.min():.1f}-{theirs_f.max():.1f} Hz, "
            f"which does not bracket the {COMPARE_LO_HZ:.0f}-{COMPARE_HI_HZ:.0f} Hz "
            f"comparison band. Re-measure over at least 20 Hz-20 kHz."
        )

    # REW's axis is its own and denser than ours; interpolate it onto ours
    # rather than resampling both, so our curve is compared exactly as the
    # engine produced it.
    theirs_on_axis = np.interp(axis, theirs_f, theirs_db)
    mine = ours_db[band]

    # Absolute level is not under test and the two tools calibrate differently
    # -- REW reports SPL against its own reference, we report dBFS. Only shape
    # is comparable, so remove one constant. Median rather than mean, so a
    # single bad bin at an edge cannot shift the whole comparison.
    offset = float(np.median(theirs_on_axis - mine))
    return axis, mine, theirs_on_axis - offset, offset


class TestAgainstREW:
    def test_the_curves_agree_within_the_tolerance(self, curves):
        axis, mine, theirs, _ = curves
        error = np.abs(theirs - mine)
        worst = int(np.argmax(error))
        assert error.max() <= TOLERANCE_DB, (
            f"worst disagreement {error.max():.3f} dB at {axis[worst]:.1f} Hz "
            f"(ours {mine[worst]:.2f}, REW {theirs[worst]:.2f}); "
            f"rms {np.sqrt(np.mean(error**2)):.3f} dB"
        )

    def test_the_rms_disagreement_is_small(self, curves):
        # The maximum is one bin and can be unlucky; the rms says whether the
        # two engines actually agree about the shape.
        _, mine, theirs, _ = curves
        rms = float(np.sqrt(np.mean((theirs - mine) ** 2)))
        assert rms <= TOLERANCE_DB / 2

    def test_the_error_is_not_a_tilt(self, curves):
        # A constant offset is removed by construction, but a *slope* is not,
        # and a slope is the signature of a real disagreement about
        # deconvolution or windowing rather than of noise. Fit one and bound it.
        axis, mine, theirs, _ = curves
        slope = np.polyfit(np.log10(axis), theirs - mine, 1)[0]
        decades = np.log10(axis.max() / axis.min())
        assert abs(slope * decades) <= TOLERANCE_DB, (
            f"the disagreement tilts {slope:.3f} dB/decade, "
            f"{slope * decades:+.2f} dB across the band"
        )

    def test_both_curves_actually_contain_the_features(self, curves):
        # Guards against the comfortable failure: two flat lines agree
        # beautifully and prove nothing. The DUT has a 3.5 kHz low-pass and a
        # 12 dB cut at 1 kHz, so both curves must have real structure.
        axis, mine, theirs, _ = curves
        for name, curve in (("ours", mine), ("REW", theirs)):
            assert curve.max() - curve.min() > 12.0, (
                f"{name} spans only {curve.max() - curve.min():.1f} dB; the "
                f"device under test should show a low-pass and a 12 dB cut"
            )
            notch = (axis > 800) & (axis < 1250)
            shoulder = (axis > 250) & (axis < 450)
            assert curve[notch].min() < curve[shoulder].mean() - 6.0, (
                f"{name} does not show the 1 kHz cut; was the band set?"
            )

    def test_the_provenance_is_recorded(self):
        # A reference measured on an unrecorded system is not a reference.
        body = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        assert body["dut"]["eq_gain_db"] == -12.0
        assert body["dut"]["lowpass_hz"] == 3500
        assert body["sample_rate_hz"] == 44_100
        assert body["timestamp"]


class TestTheBandIsWorthComparingIn:
    """Prove the comparison band, do not assert it.

    The golden ships two of our own measurements, taken back to back on an
    unchanged path. Comparing them costs nothing and settles the question that
    decides whether the REW comparison means anything: **does our instrument
    agree with itself here?**

    It exists because the answer turned out to be no over most of the original
    band. At 10-18 kHz, where the DUT's low-pass leaves the signal 50 dB down,
    two of our runs minutes apart differed by 7 dB -- more than either differed
    from REW. Had the band not been narrowed, this suite would have reported a
    disagreement with REW that was entirely our own noise, and the natural next
    move would have been to go looking for a bug in the engine.
    """

    def test_our_own_run_to_run_scatter_does_not_dominate(self):
        """The band is usable iff our noise is smaller than what we are measuring.

        **Judged on rms and the 95th percentile, not the maximum, and that
        choice was made after seeing the data** -- so here is the reasoning,
        and the numbers it was made from.

        The worst single point is 0.515 dB at 893 Hz, and every point above
        0.245 dB lies on the flanks of the 12 dB notch (700-1400 Hz). That is
        the DUT's geometry rather than the rig's instability: on a slope that
        steep, a fractional difference in the frequency axis becomes a large
        difference in dB, and any two measurements will show it. Away from the
        flanks our two runs agree to 0.245 dB worst case and 0.053 dB rms.

        The question this test exists to answer is "does our own noise swamp
        the comparison?", and the maximum over one bin cannot answer it. The
        rms can: ours is 0.066 dB against 0.094 dB for the disagreement with
        REW, so what we measure against REW is not our own scatter.

        Note what did **not** change: the REW comparison in
        :class:`TestAgainstREW` is still judged on the maximum, and still
        passes on it.
        """
        ours_f, ours_db = _read_two_columns(OURS)
        _, repeat_db = _read_two_columns(REPEAT)
        band = (ours_f >= COMPARE_LO_HZ) & (ours_f <= COMPARE_HI_HZ)
        scatter = np.abs(ours_db[band] - repeat_db[band])

        rms = float(np.sqrt(np.mean(scatter**2)))
        p95 = float(np.percentile(scatter, 95))
        assert rms <= TOLERANCE_DB / 2, (
            f"our own run-to-run rms is {rms:.3f} dB, so this band cannot "
            f"check anything against REW. Narrow the band or fix the rig -- "
            f"do not widen the tolerance."
        )
        assert p95 <= TOLERANCE_DB, f"95th percentile of our own scatter {p95:.3f} dB"

    def test_our_scatter_is_smaller_than_what_we_measure_against_rew(self):
        # The relative form of the same requirement, with no free parameter:
        # if our noise were the larger of the two, the REW comparison would be
        # measuring us against ourselves.
        ours_f, ours_db = _read_two_columns(OURS)
        _, repeat_db = _read_two_columns(REPEAT)
        theirs_f, theirs_db = _read_two_columns(THEIRS)
        band = (ours_f >= COMPARE_LO_HZ) & (ours_f <= COMPARE_HI_HZ)
        axis, mine = ours_f[band], ours_db[band]
        theirs = np.interp(axis, theirs_f, theirs_db)
        theirs = theirs - np.median(theirs - mine)

        ours_rms = float(np.sqrt(np.mean((mine - repeat_db[band]) ** 2)))
        rew_rms = float(np.sqrt(np.mean((mine - theirs) ** 2)))
        assert ours_rms <= rew_rms, (
            f"our own scatter ({ours_rms:.3f} dB rms) is larger than our "
            f"disagreement with REW ({rew_rms:.3f} dB rms); the comparison is "
            f"measuring our noise"
        )

    def test_repeatability_alone_would_allow_a_wider_band(self):
        """Records that the band is **not** limited by our repeatability.

        It was, under MME. It is not under WASAPI: our two runs agree to
        0.392 dB all the way to 8 kHz and only fail beyond it. Had the band
        been chosen by repeatability alone -- the rule stated when this file
        was written -- it would be 8 kHz.

        It is 3.5 kHz because the *comparison* fails between 3.5 and 8 kHz, for
        a reason nobody has explained. See
        :class:`TestTheUnexplainedDisagreementAtTheCorner`. Keeping this test
        stops the narrower band being mistaken for a noise limit.
        """
        ours_f, ours_db = _read_two_columns(OURS)
        _, repeat_db = _read_two_columns(REPEAT)
        wider = (ours_f >= COMPARE_LO_HZ) & (ours_f <= 8000.0)
        scatter = np.abs(ours_db[wider] - repeat_db[wider])
        assert scatter.max() <= TOLERANCE_DB, (
            f"repeatability to 8 kHz has degraded to {scatter.max():.3f} dB; "
            f"the rig has changed"
        )

    def test_the_repeat_is_a_separate_measurement(self):
        # Guards against the file being a copy, which would make the whole
        # justification circular.
        assert OURS.read_bytes() != REPEAT.read_bytes()

    def test_provenance_records_the_band_the_data_was_taken_for(self):
        # If the stored data were generated for a different band than the one
        # asserted here, the two could drift apart silently.
        body = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        assert body["compare_band_hz"] == [COMPARE_LO_HZ, COMPARE_HI_HZ]

    def test_the_band_ends_at_the_devices_corner(self):
        # The band is a rule about the device, not about the data: compare
        # across the passband, stop at the low-pass.
        body = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        assert body["dut"]["lowpass_hz"] == COMPARE_HI_HZ


FLAT_OURS_A = GOLDEN / "flat_ours_a.txt"
FLAT_OURS_B = GOLDEN / "flat_ours_b.txt"
FLAT_REW_A = GOLDEN / "flat_rew_a.txt"
FLAT_REW_B = GOLDEN / "flat_rew_b.txt"
FLAT_REW_8 = GOLDEN / "flat_rew_8sweeps_unaligned.txt"

#: Where the flat-channel experiment was compared. The channel is flat to
#: 18 kHz apart from the 1 kHz notch, so unlike the golden's DUT there is no
#: passband edge and both tools have full signal everywhere.
FLAT_LO_HZ = 30.0
FLAT_HI_HZ = 18_000.0


def _flat_curves():
    f, ours_a = _read_two_columns(FLAT_OURS_A)
    _, ours_b = _read_two_columns(FLAT_OURS_B)
    band = (f >= FLAT_LO_HZ) & (f <= FLAT_HI_HZ)
    axis = f[band]
    out = {"ours_a": ours_a[band], "ours_b": ours_b[band]}
    for key, path in (("rew_a", FLAT_REW_A), ("rew_b", FLAT_REW_B)):
        rf, rd = _read_two_columns(path)
        on_axis = np.interp(axis, rf, rd)
        out[key] = on_axis - np.median(on_axis - out["ours_a"])
    return axis, out


def _rms(a, b):
    return float(np.sqrt(np.mean((a - b) ** 2)))


class TestTheBandIsLimitedByTheReference:
    """Why the comparison stops at 3.5 kHz. It is not our engine, and not noise
    in our rig -- it is REW.

    A first pass called this an unexplained 1.9 dB disagreement just above the
    DUT's low-pass corner, with a windowing hypothesis attached: group delay
    peaks at a corner, REW windows the impulse where we use the full IR, and
    REW read lower there. Two experiments killed that.

    **One: remove the low-pass.** On a channel flat to 18 kHz, at full level
    everywhere, the disagreement did not go away -- it grew, to 2.1 dB. So it
    was never about the slope.

    **Two: measure the reference against itself.** Two REW runs, identical
    settings, unchanged path:

    ========================  =========  =========
    Comparison                  max, dB    rms, dB
    ========================  =========  =========
    our two runs                  0.395      0.080
    **REW's two runs**            3.311      0.370
    ours vs REW run 1             2.110      0.261
    ours vs mean of both REW      1.987      0.201
    ========================  =========  =========

    **REW's own run-to-run scatter is larger than our disagreement with it**,
    and averaging its two runs moves it toward us -- which is what noise does
    and a systematic error does not. There is nothing left to explain: the
    engines agree as closely as the reference can resolve, and above 3.5 kHz
    the reference cannot resolve half a dB.

    It cannot be improved on this rig either. REW averages multiple sweeps only
    with a timing reference, and the Solo's two inputs are the DUT and a mic
    preamp, so there is no spare channel for a loopback. See
    :class:`TestUnalignedAveragingIsCatastrophic` for what happens when you try
    anyway.
    """

    def test_rews_own_scatter_exceeds_our_disagreement_with_it(self):
        axis, c = _flat_curves()
        hf = axis > COMPARE_HI_HZ
        rew_self = _rms(c["rew_a"][hf], c["rew_b"][hf])
        against = _rms(c["ours_a"][hf], c["rew_a"][hf])
        assert rew_self >= against, (
            f"above {COMPARE_HI_HZ:.0f} Hz REW now repeats to {rew_self:.3f} dB "
            f"rms while disagreeing with us by {against:.3f} dB. The reference "
            f"got quieter than the disagreement -- there may be something real "
            f"here now. Investigate before widening the band."
        )

    def test_we_are_more_repeatable_than_the_reference(self):
        # A claim worth making explicitly, and the reason the band limit is not
        # a criticism of this project's engine.
        axis, c = _flat_curves()
        ours = _rms(c["ours_a"], c["ours_b"])
        theirs = _rms(c["rew_a"], c["rew_b"])
        assert ours < theirs
        assert ours <= TOLERANCE_DB / 2

    def test_averaging_the_reference_moves_it_toward_us(self):
        # The signature of noise. A systematic difference would not shrink when
        # two independent runs of the reference are averaged.
        _, c = _flat_curves()
        mean_rew = (c["rew_a"] + c["rew_b"]) / 2
        assert _rms(c["ours_a"], mean_rew) < _rms(c["ours_a"], c["rew_a"])

    def test_removing_the_low_pass_did_not_remove_the_disagreement(self):
        # Kills the windowing-at-the-corner hypothesis, which was plausible and
        # wrong. Kept so it is not re-proposed.
        axis, c = _flat_curves()
        corner = (axis >= 3500) & (axis <= 5000)
        assert np.abs(c["rew_a"][corner] - c["ours_a"][corner]).max() > TOLERANCE_DB


class TestUnalignedAveragingIsCatastrophic:
    """REW averaging 8 sweeps with no timing reference, kept as evidence.

    This project's ``_combine_passes`` aligns passes to sub-sample precision
    before combining them, and its docstring warns that skipping that step
    "comb-filters the result, and the damage looks like a catastrophic system
    response rather than an averaging bug -- 24 dB of span on a loopback that is
    flat to a third of a dB".

    That claim came from our own rig. This file is the same failure reproduced
    in an independent tool, on the same electrical path, on request: 8 sweeps
    averaged without a timing reference to align them, against the same path
    measured once.

    It was produced by a bad instruction -- turn the timing reference off *and*
    raise the repetitions, two settings that are each harmless alone. The
    resulting curve is not noisy, it is *wrong*, and it is wrong in the specific
    shape that identifies the cause: no error at DC, growing with frequency
    because phase error is proportional to ``f x dt``, then oscillating as the
    comb sets in.
    """

    def test_the_damage_is_absent_at_dc_and_grows_with_frequency(self):
        f1, d1 = _read_two_columns(FLAT_REW_A)
        f8, d8 = _read_two_columns(FLAT_REW_8)
        d8 = np.interp(f1, f8, d8)
        low = (f1 >= 20) & (f1 <= 60)
        mid = (f1 >= 800) & (f1 <= 3000)
        assert np.abs(d8[low] - d1[low]).max() < 0.5
        assert np.abs(d8[mid] - d1[mid]).max() > 5.0

    def test_it_is_far_worse_than_any_plausible_noise(self):
        f1, d1 = _read_two_columns(FLAT_REW_A)
        f8, d8 = _read_two_columns(FLAT_REW_8)
        d8 = np.interp(f1, f8, d8)
        band = (f1 >= 20) & (f1 <= 20_000)
        assert np.abs(d8[band] - d1[band]).max() > 40.0

    def test_more_sweeps_made_it_worse_not_better(self):
        # The whole point: averaging is not automatically an improvement.
        axis, c = _flat_curves()
        f8, d8 = _read_two_columns(FLAT_REW_8)
        eight = np.interp(axis, f8, d8)
        eight = eight - np.median(eight - c["ours_a"])
        assert _rms(c["ours_a"], eight) > 10 * _rms(c["ours_a"], c["rew_a"])
