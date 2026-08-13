"""The bring-up tool's own guards.

``tools/dsp408_probe.py`` is the only thing in this project that has ever
transmitted to the real DSP-408, so its refusals are as load-bearing as any
library code and get tested the same way. It is a script rather than a
package, hence the ``importlib`` dance.

Scope is deliberately narrow: the decisions the tool makes that no library
layer below it makes. Everything about framing, arming, read-modify-write and
restore is covered where it lives.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

from tuner.dsp.fake_device import DeviceImage, FakeDsp408

TOOL = Path(__file__).resolve().parents[1] / "tools" / "dsp408_probe.py"


def _load():
    spec = importlib.util.spec_from_file_location("dsp408_probe", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load()


def _args(tmp_path, **kw) -> argparse.Namespace:
    base = dict(
        fake=True,
        fake_from=None,
        address=None,
        port=None,
        channel=1,
        link_id=probe.OBSERVED_BLUETOOTH_DEVICE_ID,
        journal=str(tmp_path / "journal.jsonl"),
        session_id="test",
        apply=False,
        snapshot_out=str(tmp_path / "shot.json"),
        output=1,
        reason="test",
        max_writes=2,
        max_channels=1,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _fake_with_gain(raw: int):
    """A fake whose OUT1 gain_raw is ``raw`` instead of the captured 500."""
    image = DeviceImage.flat()
    start = probe.STAGE5_BLOCK * 8
    image.channels[0][start + 2 : start + 4] = raw.to_bytes(2, "little")
    return lambda: FakeDsp408(image)


class TestStage5Guard:
    """It performs one transition and refuses every other starting state.

    The precedent is specific, not general: ``gain_raw`` 500 -> 490 on OUT1 is
    a byte sequence the capture shows this device accepting from the vendor
    app. From any other starting value it would be a write nobody has seen it
    accept, and the fact that it is "only a gain" is not evidence about the
    device -- it is evidence about our decode.
    """

    def test_the_expected_start_is_the_captured_one(self):
        assert probe.STAGE5_EXPECT_RAW == 500
        assert probe.STAGE5_TARGET_RAW == 490

    def test_a_dry_run_transmits_nothing(self, tmp_path, capsys):
        assert probe.cmd_stage5(_args(tmp_path)) == 0
        assert "Dry run" in capsys.readouterr().out

    def test_it_writes_and_rolls_back(self, tmp_path, capsys):
        assert probe.cmd_stage5(_args(tmp_path, apply=True)) == 0
        out = capsys.readouterr().out
        assert "landed         True" in out
        assert "neighbours     preserved" in out
        assert "after rollback: identical" in out

    @pytest.mark.parametrize("raw", [490, 480, 0, 600])
    def test_it_refuses_an_unexpected_starting_gain(
        self, tmp_path, monkeypatch, capsys, raw
    ):
        monkeypatch.setattr(probe, "FakeDsp408", _fake_with_gain(raw))
        assert probe.cmd_stage5(_args(tmp_path, apply=True)) == 2
        out = capsys.readouterr().out
        assert "REFUSED" in out
        assert "Nothing was transmitted" in out

    def test_the_refusal_happens_before_arming(self, tmp_path, monkeypatch):
        # Not merely "no write went out" -- the run must not even reach the
        # armed state, so there is no window in which a later bug could
        # transmit.
        monkeypatch.setattr(probe, "FakeDsp408", _fake_with_gain(480))
        armed = []
        original = probe.Dsp408Device.arm_writes
        monkeypatch.setattr(
            probe.Dsp408Device,
            "arm_writes",
            lambda self, *a, **k: (armed.append(1), original(self, *a, **k))[1],
        )
        assert probe.cmd_stage5(_args(tmp_path, apply=True)) == 2
        assert armed == []

    def test_the_restore_point_exists_before_any_write(self, tmp_path):
        # Arming verifies a snapshot, but only this proves the file is on disk
        # and readable by the time the write goes out -- an in-memory snapshot
        # is not a restore point if the process dies.
        out = tmp_path / "shot.json"
        probe.cmd_stage5(_args(tmp_path, apply=True, snapshot_out=str(out)))
        assert out.is_file() and out.stat().st_size > 0


class TestNoopWriteGuard:
    def test_a_dry_run_transmits_nothing(self, tmp_path, capsys):
        assert probe.cmd_noop_write(_args(tmp_path, block=31)) == 0
        assert "Dry run" in capsys.readouterr().out

    def test_apply_leaves_the_device_byte_identical(self, tmp_path, capsys):
        assert probe.cmd_noop_write(_args(tmp_path, apply=True, block=31)) == 0
        assert "byte-identical" in capsys.readouterr().out
