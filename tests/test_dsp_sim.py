"""Tests for the simulated DSP backend.

The simulator's purpose is to enforce the same resource budget the real chip
does, so that code passing here does not discover on first contact with
hardware that its tune does not fit.
"""

import pytest

from tuner.dsp import (
    Biquad,
    BudgetExceeded,
    ChannelConfig,
    DeviceLimits,
    SimulatedDsp,
)


@pytest.fixture
def dsp():
    with SimulatedDsp() as d:
        yield d


class TestConnection:
    def test_access_before_connect_raises(self):
        with pytest.raises(RuntimeError, match="not connected"):
            SimulatedDsp().read_channel(0)

    def test_context_manager_connects_and_disconnects(self):
        d = SimulatedDsp()
        with d:
            d.read_channel(0)
        with pytest.raises(RuntimeError):
            d.read_channel(0)


class TestChannelAccess:
    def test_defaults_are_flat_and_silent(self, dsp):
        ch = dsp.read_channel(0)
        assert ch.gain_dbfs == 0.0
        assert ch.delay_samples == 0
        assert ch.peq == ()

    def test_roundtrip(self, dsp):
        cfg = ChannelConfig(gain_dbfs=-3.0, delay_samples=48)
        dsp.write_channel(2, cfg)
        assert dsp.read_channel(2) == cfg

    def test_out_of_range_output_raises(self, dsp):
        with pytest.raises(IndexError):
            dsp.read_channel(99)


class TestResourceBudget:
    def test_too_many_peq_bands_raises(self, dsp):
        bands = tuple(
            Biquad(freq_hz=100.0 * i, gain_dbfs=-1.0, q=1.0) for i in range(1, 12)
        )
        with pytest.raises(BudgetExceeded, match="PEQ bands"):
            dsp.write_channel(0, ChannelConfig(peq=bands))

    def test_delay_is_capped_per_output(self, dsp):
        # Measured 2026-08-12: 8 ms per channel = 384 samples at 48 kHz.
        cap = dsp.limits.max_delay_samples_per_output
        dsp.write_channel(0, ChannelConfig(delay_samples=cap))
        assert dsp.read_channel(0).delay_samples == cap
        with pytest.raises(BudgetExceeded, match="per-output ceiling"):
            dsp.write_channel(0, ChannelConfig(delay_samples=cap + 1))

    def test_outputs_do_not_share_a_pool(self, dsp):
        # **This is the test that used to assert the opposite.** It read
        # "the per-channel API makes outputs look independent; the silicon
        # does not", and pinned a 1024-sample per-chip pool that does not
        # exist. The operator settled it in about a minute: the vendor app
        # offers its full 8 ms on every output at once, which a shared pool
        # could not do.
        cap = dsp.limits.max_delay_samples_per_output
        for output in range(dsp.limits.n_outputs):
            dsp.write_channel(output, ChannelConfig(delay_samples=cap))
        assert all(
            dsp.read_channel(o).delay_samples == cap
            for o in range(dsp.limits.n_outputs)
        )
        assert dsp.total_delay_samples() == cap * dsp.limits.n_outputs

    def test_chip_mates_do_not_constrain_each_other(self, dsp):
        # Outputs 1 and 2 share the ADAU at 0x37. Under the old model, both at
        # the cap was an error; the device runs it.
        assert dsp.limits.chip_of(0) == dsp.limits.chip_of(1)
        cap = dsp.limits.max_delay_samples_per_output
        dsp.write_channel(0, ChannelConfig(delay_samples=cap))
        dsp.write_channel(1, ChannelConfig(delay_samples=cap))
        assert dsp.delay_samples_on(0) == 2 * cap

    def test_rejected_write_leaves_state_unchanged(self, dsp):
        cap = dsp.limits.max_delay_samples_per_output
        dsp.write_channel(1, ChannelConfig(delay_samples=10))
        with pytest.raises(BudgetExceeded):
            dsp.write_channel(1, ChannelConfig(delay_samples=cap + 1))
        assert dsp.read_channel(1).delay_samples == 10

    def test_negative_delay_raises(self, dsp):
        with pytest.raises(ValueError):
            dsp.write_channel(0, ChannelConfig(delay_samples=-1))

    def test_total_delay_accounting(self, dsp):
        dsp.write_channel(0, ChannelConfig(delay_samples=100))
        dsp.write_channel(1, ChannelConfig(delay_samples=250))
        assert dsp.total_delay_samples() == 350


class TestLimits:
    def test_the_limits_are_measured(self):
        # Flipped 2026-08-12. Every field is now measured: channel counts from
        # the wire, the chip map on a logic analyser, and both ceilings from
        # the vendor UI. Program space is absent by decision -- it is not a
        # resource we allocate on fixed vendor firmware -- rather than by
        # omission, which is why its absence no longer forces measured=False.
        assert DeviceLimits().measured is True

    def test_the_delay_ceiling_is_eight_milliseconds(self):
        limits = DeviceLimits()
        assert limits.max_delay_samples_per_output == 384
        assert limits.max_delay_samples_per_output / limits.sample_rate_hz == 0.008

    def test_ten_peq_bands_is_what_the_vendor_exposes(self):
        # Not a conservative placeholder any more. The device addresses 31
        # slots and stores all of them; the app offers 10, and whether 11-30
        # are live in firmware is an open question with a planned experiment.
        assert DeviceLimits().max_peq_per_channel == 10

    def test_sample_rate_is_fixed_at_48k(self):
        assert DeviceLimits().sample_rate_hz == 48_000
