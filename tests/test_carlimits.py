"""The car's driver limits refuse what would destroy a driver.

Every fatal case here corresponds to something ``docs/car-system.md`` says
kills hardware on this vehicle, because the drivers have no passive networks.
"""

import pytest

from tuner.dsp.backend import ChannelConfig, Crossover
from tuner.dsp.snapshot import SYSTEM_MASTER_VOLUME
from tuner.optimize.biquad import Biquad
from tuner.orchestrate.carlimits import (
    CAR_DSP408,
    INPUT_GAIN_MAX,
    MASTER_VOLUME_MAX,
    SPEC_BY_OUTPUT,
    check_channel,
    check_master_volume,
    require_declared_input_gain,
    slope_db_oct,
)

TWEETER = SPEC_BY_OUTPUT[2]        # OUT3, Audiofrog GB10
MID = SPEC_BY_OUTPUT[0]            # OUT1, FaitalPRO 4FE35
SUB = SPEC_BY_OUTPUT[6]            # OUT7


def cfg(hp=3500.0, lp=20000.0, slope=24, gain=-10.0, peq=()):
    return ChannelConfig(
        gain_dbfs=gain,
        crossover=Crossover(high_pass_hz=hp, low_pass_hz=lp, slope_db_oct=slope),
        peq=tuple(peq),
    )


def fatal(vs):
    return [v for v in vs if v.fatal]


class TestSpecTable:
    def test_covers_all_eight_outputs(self):
        assert sorted(s.output for s in CAR_DSP408) == list(range(8))

    def test_every_spec_carries_a_basis(self):
        """Unverifiable by code, so it must at least be written down."""
        for s in CAR_DSP408:
            assert s.basis.strip(), f"{s.label} has no basis"

    def test_slope_encoding(self):
        assert slope_db_oct(1) == 12
        assert slope_db_oct(3) == 24


class TestTheTweeterThatDiesFirst:
    def test_correct_configuration_passes(self):
        assert not fatal(check_channel(TWEETER, cfg(), require_flat=False))

    def test_a_high_pass_opened_to_20_hz_is_refused(self):
        """What bench_flatten does, and what would end the GB10s."""
        vs = fatal(check_channel(TWEETER, cfg(hp=20.0), require_flat=False))
        assert vs and "high-pass" in vs[0].message

    def test_a_disabled_high_pass_is_refused(self):
        vs = fatal(check_channel(TWEETER, cfg(hp=None), require_flat=False))
        assert vs and "DISABLED" in vs[0].message

    def test_slope_below_the_manufacturer_floor_is_refused(self):
        vs = fatal(check_channel(TWEETER, cfg(slope=6), require_flat=False))
        assert vs and "floor" in vs[0].message

    def test_twelve_db_per_octave_warns_but_does_not_refuse(self):
        """12 is the stated minimum, so it is legal and still not enough.

        Refusing it would be wrong -- it is what the car is set to today --
        but passing it silently would lose the excursion argument.
        """
        vs = check_channel(TWEETER, cfg(slope=12), require_flat=False)
        assert not fatal(vs)
        assert any("recommended" in v.message for v in vs)


class TestTheMidsThatTonightsBenchWorkEndangered:
    def test_the_current_bench_state_is_refused_for_the_car(self):
        """OUT1/2 were opened to 20-20000 for a self-protected plate amp.

        In the car those outputs are 4-inch drivers with 1.73 mm of Xmax.
        This is the specific state the DSP is in as it goes back to the car.
        """
        vs = fatal(check_channel(MID, cfg(hp=20.0, lp=20000.0), require_flat=False))
        assert len(vs) == 2
        assert any("high-pass" in v.message for v in vs)
        assert any("low-pass" in v.message for v in vs)

    def test_the_car_configuration_passes(self):
        assert not fatal(
            check_channel(MID, cfg(hp=450.0, lp=3500.0, slope=12),
                          require_flat=False)
        )


class TestGain:
    def test_gain_above_the_clip_point_is_refused(self):
        vs = fatal(check_channel(TWEETER, cfg(gain=-4.0), require_flat=False))
        assert vs and "ceiling" in vs[0].message

    def test_exactly_the_clip_point_passes(self):
        assert not fatal(check_channel(TWEETER, cfg(gain=-6.0), require_flat=False))


class TestFlatBaseline:
    def test_loaded_eq_is_refused_when_a_baseline_is_wanted(self):
        band = Biquad(freq_hz=1000.0, gain_dbfs=-6.0, q=2.0)
        vs = fatal(check_channel(TWEETER, cfg(peq=[band]), require_flat=True))
        assert vs and "baseline" in vs[0].message

    def test_loaded_eq_is_fine_when_it_is_not_a_baseline(self):
        band = Biquad(freq_hz=1000.0, gain_dbfs=-6.0, q=2.0)
        assert not fatal(check_channel(TWEETER, cfg(peq=[band]), require_flat=False))

    def test_a_band_at_zero_db_does_not_count_as_loaded(self):
        band = Biquad(freq_hz=1000.0, gain_dbfs=0.0, q=2.0)
        assert not fatal(check_channel(TWEETER, cfg(peq=[band]), require_flat=True))


def master_block(level: int) -> dict[int, bytes]:
    """The live block read from the car's DSP on 2026-08-14, level substituted."""
    return {SYSTEM_MASTER_VOLUME: bytes([level, 0, 1, 50, 0, 50, 1, 0])}


class TestMasterVolume:
    def test_at_the_limit_passes(self):
        assert not check_master_volume(master_block(MASTER_VOLUME_MAX))

    def test_above_the_limit_is_refused(self):
        vs = check_master_volume(master_block(MASTER_VOLUME_MAX + 1))
        assert vs and vs[0].fatal

    def test_a_missing_block_is_refused_rather_than_assumed_fine(self):
        vs = check_master_volume({})
        assert vs and vs[0].fatal


class TestInputGain:
    def test_undeclared_is_refused(self):
        """Unknown must not read as fine. We cannot see this value at all."""
        vs = require_declared_input_gain(None)
        assert vs and vs[0].fatal
        assert "DataType 3" in vs[0].message

    def test_above_the_limit_is_refused(self):
        vs = require_declared_input_gain(INPUT_GAIN_MAX + 1)
        assert vs and vs[0].fatal

    def test_at_the_limit_passes(self):
        assert not require_declared_input_gain(INPUT_GAIN_MAX)


class TestTheSubsonicFilterSitsBelowPortTuning:
    """The box is tuned to 32 Hz and the subsonic filter is at 20 Hz.

    Below tuning a ported box unloads and excursion runs away, so the 12 Hz
    between them is the region with the least mechanical control and almost
    no electrical attenuation. It warns rather than refuses: the car has run
    this way for years and blocking a session over it would be wrong, but it
    must not pass silently either.
    """

    def test_the_car_as_configured_warns(self):
        c = cfg(hp=20.0, lp=65.0, slope=12, gain=-19.0)
        vs = check_channel(SUB, c, require_flat=False)
        assert not fatal(vs)
        assert any("port tuning" in v.message for v in vs)

    def test_at_port_tuning_it_stops_warning_about_the_corner(self):
        c = cfg(hp=32.0, lp=65.0, slope=24, gain=-19.0)
        vs = check_channel(SUB, c, require_flat=False)
        assert not any("recommended" in v.message for v in vs)

    def test_the_recommendation_carries_its_own_citation(self):
        assert "32 Hz" in SUB.recommendation_basis
        assert SUB.recommended_high_pass_hz == 32


@pytest.mark.parametrize("spec", CAR_DSP408, ids=lambda s: s.label)
def test_each_channels_declared_configuration_passes_its_own_spec(spec):
    """The table must not refuse the car it describes."""
    c = cfg(
        hp=float(spec.high_pass_hz),
        lp=float(spec.low_pass_hz),
        slope=spec.min_slope_db_oct,
        gain=spec.max_gain_dbfs,
    )
    assert not fatal(check_channel(spec, c, require_flat=False))
