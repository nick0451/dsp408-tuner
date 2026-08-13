"""Rehearse the closed tuning loop, or inspect a plan, with no hardware.

``rehearse`` drives the whole of M4 -- arm, floor, baseline, fit, write,
verify, settle -- against the in-process fake DSP and a synthetic rig, once
for each outcome the run can produce. The bench should not be the first time
any of this executes, and neither should the car::

    python tools/tune_run.py rehearse
    python tools/tune_run.py rehearse --verbose

``plan`` prints a plan's fingerprint and its canonical form, which is what
gets recorded in provenance. Use it to confirm two runs were judged by the
same objective, or to see what changing a weight does to the hash::

    python tools/tune_run.py plan --slot 6 --confirmed-by "operator, bench"

``measure`` and ``predict-check`` **do** talk to a real DSP-408 and a real
interface. They are the bench half of this tool::

    python tools/tune_run.py measure --address <MAC> --output 1 --level-dbfs -20
    python tools/tune_run.py predict-check --address <MAC> \
        --snapshot-out snapshots/<date>.json [--apply]

``measure`` establishes the session's linearity and repeatability floor, which
the improvement invariant needs and which does not survive between sessions.
``predict-check`` writes one known EQ band through this project's own backend
and compares the measured result against ``biquad.response_db`` -- the
experiment that rules out an optimizer fitting a model the device no longer
matches. Both are read-only unless ``--apply`` is given, both snapshot first,
and ``predict-check`` rolls back in a ``finally``.

:class:`tuner.orchestrate.rig.AcousticMeasurer` has still never run against a
rig; these two use ``capture_sweep`` directly.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tuner.dsp.device import Dsp408Device, WriteJournal  # noqa: E402
from tuner.dsp.dsp408_spp import Dsp408Spp, PeqPolicy  # noqa: E402
from tuner.dsp.fake_device import FakeDsp408  # noqa: E402
from tuner.dsp.session import Dsp408Session, Pacing  # noqa: E402
from tuner.dsp.transport import LoopbackTransport  # noqa: E402
from tuner.dsp.txpolicy import BlastRadius, TxPolicy  # noqa: E402
from tuner.measure.qa import SilentPath  # noqa: E402
from tuner.measure.result import (  # noqa: E402
    Coupling,  # noqa: E402
    Measurement,
    Provenance,
)
from tuner.optimize import biquad  # noqa: E402
from tuner.optimize.target import flat  # noqa: E402
from tuner.optimize.verify import Outcome  # noqa: E402
from tuner.orchestrate import (  # noqa: E402
    IsolationError,
    MagnitudeObjective,
    MuteIsolator,
    NoIsolation,
    RollbackFailed,
    TunePlan,
    TuneRun,
)
from tuner.orchestrate.plan import DriverCeiling, Gang  # noqa: E402
from tuner.safety.limits import DEFAULT_CEILING_DBFS  # noqa: E402

SAMPLE_RATE_HZ = 48_000
AXIS = np.geomspace(30.0, 16_000.0, 200)
N_FFT = 16_384


# -- the synthetic world ----------------------------------------------------


def speaker_db(freqs_hz: np.ndarray) -> np.ndarray:
    """A speaker with a broad suck-out and a narrow peak. Both correctable."""
    f = np.maximum(np.asarray(freqs_hz, dtype=np.float64), 1.0)
    return -6.0 * np.exp(-((np.log2(f / 300.0) / 1.2) ** 2)) + 5.0 * np.exp(
        -((np.log2(f / 3_000.0) / 0.35) ** 2)
    )


def impulse_from(mag_db: np.ndarray) -> np.ndarray:
    return np.fft.irfft(10.0 ** (mag_db / 20.0), N_FFT)


@dataclass
class SyntheticRig:
    """Reads the DSP and returns what that configuration would sound like."""

    backend: Dsp408Spp
    deaf: bool = False
    noise_db: float = 0.02
    temperature_c: float = 21.0
    setup_token: str | None = "rehearsal: synthetic rig, nothing moves"
    warm_on_verify: bool = False
    die_on_verify: bool = False
    deaf_to_mute: bool = False
    sweeps: int = field(default=0, init=False)
    calls: list = field(default_factory=list, init=False)
    _seed: int = field(default=0, init=False)

    def measure(self, output, limit, tag):
        if self.die_on_verify and tag == "verify":
            raise OSError("the interface went away mid-run")
        if self.warm_on_verify and tag == "verify":
            self.temperature_c = 40.0
        # A muted output makes no sound, and capture_sweep's safety ramp
        # raises SilentPath when the stimulus does not arrive. Modelling it
        # here is what makes the silence proof a real test rather than a stub.
        if not self.deaf_to_mute and self.backend.read_channel(output).muted:
            raise SilentPath(f"output {output} is muted")

        self.sweeps += 1
        self.calls.append((output, limit.ceiling_dbfs, tag))
        self._seed += 1
        freqs = np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE_HZ)
        mag = speaker_db(freqs)
        if not self.deaf:
            config = self.backend.read_channel(output)
            mag = mag + biquad.response_db(tuple(config.peq), freqs, SAMPLE_RATE_HZ)
        mag = mag + np.random.default_rng(self._seed).normal(
            0.0, self.noise_db, mag.shape
        )
        return [
            Measurement(
                impulse=impulse_from(mag),
                provenance=Provenance(
                    device="synthetic rig",
                    sample_rate_hz=SAMPLE_RATE_HZ,
                    gains_db=(30.0,),
                    timestamp=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
                    temperature_c=self.temperature_c,
                    setup_token=self.setup_token,
                ),
            )
        ]


def build(workdir: Path, slot: int = 6) -> tuple[Dsp408Spp, TunePlan]:
    fake = FakeDsp408()
    session = Dsp408Session(
        LoopbackTransport(fake),
        policy=TxPolicy(
            allow_writes=True,
            allow_presets=True,
            blast_radius=BlastRadius(max_writes=4000, max_channels=8),
        ),
        pacing=Pacing(idle_after_reply_s=0.0, max_requests_per_s=1e9),
    )
    device = Dsp408Device(
        session,
        journal=WriteJournal(workdir / "journal.jsonl"),
        session_id="rehearsal",
    )
    backend = Dsp408Spp(device=device, peq_policy=PeqPolicy.EXCLUSIVE)
    backend.connect()
    return backend, make_plan(workdir / "baseline.json", slot)


def make_plan(snapshot_path: Path, slot: int, confirmed_by: str = "") -> TunePlan:
    objective = MagnitudeObjective(
        name="rehearsal-flat-rms",
        target=flat(np.array([20.0, 20_000.0]), name="flat"),
        freqs_hz=AXIS,
        source_weights={0: 1.0},
        position_weights=(1.0,),
    )
    return TunePlan(
        session_id="rehearsal",
        objective=objective,
        scratch_slot=slot,
        scratch_slot_confirmed_by=(
            confirmed_by or "rehearsal only -- no real slot is touched"
        ),
        scratch_slot_holds="(rehearsal, in-process fake)",
        snapshot_path=snapshot_path,
        constraints=replace(biquad.DEFAULT_CONSTRAINTS, max_bands=6),
        driver_ceilings={
            0: DriverCeiling(-12.0, "rehearsal: nothing is connected to anything")
        },
    )


#: Outputs 7 and 8 drive two subwoofers in one ported box. They are one
#: acoustic source to measure and one correction to write, and mismatching
#: them is a mechanical hazard rather than a tonal choice.
SUBS = Gang(outputs=(6, 7), basis="two subwoofers in one ported box", name="subs")


def make_ganged_plan(snapshot_path: Path, slot: int = 6) -> TunePlan:
    plan = make_plan(snapshot_path, slot)
    return replace(
        plan,
        objective=replace(plan.objective, source_weights={0: 1.0, 6: 1.0}),
        gangs=(SUBS,),
        driver_ceilings={
            0: DriverCeiling(-12.0, "rehearsal: nothing is connected to anything"),
            6: DriverCeiling(-12.0, "rehearsal: nothing is connected to anything"),
            7: DriverCeiling(-12.0, "rehearsal: nothing is connected to anything"),
        },
    )


def link_in_the_app(backend, channels=(6, 7), group=3) -> None:
    """Set linkgroup_num the way the vendor app would."""
    fake = backend.device.session.transport.device
    for channel in channels:
        record = bytearray(fake.image.channels[channel])
        record[35 * 8 + 7] = group
        fake.image.channels[channel] = record


# -- rehearsal --------------------------------------------------------------


def rehearse(verbose: bool) -> int:
    workroot = Path(tempfile.mkdtemp(prefix="tune-rehearse-"))
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(
            f"  [{'ok' if ok else 'XX'}] {name}" + (f" -- {detail}" if detail else "")
        )

    def run_one(label: str, workdir: Path, **rig_kwargs):
        workdir.mkdir(parents=True, exist_ok=True)
        backend, plan = build(workdir)
        rig = SyntheticRig(backend, **rig_kwargs)
        report = TuneRun(plan, backend, rig, MuteIsolator(backend)).execute()
        if verbose:
            print("\n" + report.summary() + "\n")
        return backend, rig, report

    print("Stage 1 -- a tune that works, and is accepted")
    backend, rig, report = run_one("accept", workroot / "accept")
    check("outcome is ACCEPTED", report.outcome is Outcome.ACCEPTED)
    check(
        "the objective improved by more than the floor",
        report.verdict is not None and -report.verdict.delta > report.floor.value,
        f"improved {-report.verdict.delta:.3f} dB against a "
        f"{report.floor.value:.3f} dB floor",
    )
    check("the device holds the fitted bands", len(backend.read_channel(0).peq) > 0)
    check(
        "the baseline snapshot is on disk",
        (workroot / "accept" / "baseline.json").exists(),
    )
    check("writes were disarmed afterwards", backend.device.armed is False)
    check(
        "isolation was proved by measurement before anything was scored",
        report.stage_data("isolation", "proved_silent") is True,
    )
    check(
        "no channel was left muted by the tune",
        not any(backend.read_channel(ch).muted for ch in range(8)),
    )
    check(
        "the device was written once, not per fit iteration",
        report.stage_data("write", "block_writes") < 40,
        f"{report.stage_data('write', 'block_writes')} block writes",
    )

    print("\nStage 2 -- a tune the system ignores, and is rejected")
    backend, rig, report = run_one("reject", workroot / "reject", deaf=True)
    check("outcome is REJECTED", report.outcome is Outcome.REJECTED)
    check("the rollback put every byte back", report.rollback.device_matches)
    check(
        "the rollback was proved by re-measurement, not just readback",
        report.rollback_verified_acoustically is True,
    )
    check(
        "the preset path was tried first",
        report.stage_data("settle", "restore_path", "").startswith("preset"),
    )

    print("\nStage 3 -- provenance drifts, so the answer is indeterminate")
    backend, rig, report = run_one("indet", workroot / "indet", warm_on_verify=True)
    check(
        "outcome is INDETERMINATE, not pass or fail",
        report.outcome is Outcome.INDETERMINATE,
    )
    check(
        "indeterminate rolls back too",
        report.rollback is not None and report.rollback.device_matches,
    )

    print("\nStage 4 -- a run that can never reach a verdict stops before the write")
    # The lesson from the first hardware loop, 2026-08-12: that run armed,
    # measured, fitted, wrote eleven blocks, and only then found at VERIFY that
    # no thermometer reading had been supplied. Nothing was wrong with the tune;
    # the run simply could not say so, and it could have known before it changed
    # anything. A provenance that is not comparable to *itself* is structurally
    # unverifiable, whatever gets measured next.
    workdir = workroot / "no-token"
    workdir.mkdir(parents=True, exist_ok=True)
    backend, plan = build(workdir)
    before = [backend.record(ch) for ch in range(8)]
    report = TuneRun(
        plan, backend, SyntheticRig(backend, setup_token=None), MuteIsolator(backend)
    ).execute()
    stages = [record.stage.value for record in report.stages]
    check("the run does not accept", not report.accepted)
    check(
        "it stops before FIT and before WRITE",
        "fit" not in stages and "write" not in stages,
        "reached " + ", ".join(stages),
    )
    check(
        "the error names the term that is missing",
        report.error is not None and "setup token" in report.error,
    )
    check(
        "the device is byte-identical -- nothing was written to undo",
        [backend.device.refresh(ch) for ch in range(8)] == before,
    )

    print("\nStage 5 -- global device state moves behind the run's back")
    workdir = workroot / "volume"
    workdir.mkdir(parents=True, exist_ok=True)
    backend, plan = build(workdir)
    rig = SyntheticRig(backend)
    fake = backend.device.session.transport.device
    original = rig.measure

    def nudge(output, limit, tag):
        if tag == "verify":
            block = bytearray(fake.image.system[5])
            block[0] = (block[0] + 7) % 256
            fake.image.system[5] = bytes(block)
        return original(output, limit, tag)

    rig.measure = nudge
    report = TuneRun(plan, backend, rig, MuteIsolator(backend)).execute()
    if verbose:
        print("\n" + report.summary() + "\n")
    check(
        "a moved master volume is indeterminate",
        report.outcome is Outcome.INDETERMINATE,
    )
    check(
        "and the reason names it",
        "master volume" in (report.verdict.reason if report.verdict else ""),
    )

    print("\nStage 6 -- the measurement path dies mid-run")
    backend, rig, report = run_one("abort", workroot / "abort", die_on_verify=True)
    check("the run aborted rather than guessing", report.verdict is None)
    check("the error is recorded", "the interface went away" in (report.error or ""))
    check("the device was still put back", report.rollback.device_matches)
    check(
        "no acoustic verification was claimed",
        report.rollback_verified_acoustically is None,
        "None, not False: the abort may be the measurement path itself",
    )

    print("\nStage 7 -- a rollback the microphone disagrees with")
    workdir = workroot / "badroll"
    workdir.mkdir(parents=True, exist_ok=True)
    backend, plan = build(workdir)
    rig = SyntheticRig(backend, deaf=True)
    original = rig.measure

    def drift(output, limit, tag):
        result = original(output, limit, tag)
        if tag == "rollback":
            # The bytes went back; the room did not.
            return [
                replace(
                    result[0],
                    impulse=impulse_from(
                        3.0 * speaker_db(np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE_HZ))
                    ),
                )
            ]
        return result

    rig.measure = drift
    raised = False
    try:
        TuneRun(plan, backend, rig, MuteIsolator(backend)).execute()
    except RollbackFailed as exc:
        raised = True
        if verbose:
            print(f"\n  {exc}\n")
    check("a byte-clean rollback the ear rejects raises", raised)

    print("\nStage 8 -- refusals that must happen before any device contact")
    from tuner.orchestrate import OrchestrationError
    from tuner.orchestrate.plan import PlanError

    for name, thunk, expected in [
        (
            "the working area is refused as a backup slot",
            lambda: make_plan(workroot / "x.json", 0),
            PlanError,
        ),
        (
            "a slot beyond the device is refused",
            lambda: make_plan(workroot / "x.json", 15),
            PlanError,
        ),
        (
            "a run without EXCLUSIVE band policy is refused",
            lambda: _leading_run(workroot / "lead"),
            OrchestrationError,
        ),
    ]:
        try:
            thunk()
        except expected as exc:
            check(name, True, str(exc).split(";")[0][:70])
        else:
            check(name, False, "did not raise")

    print("\nStage 9 -- isolation, and what it refuses")
    workdir = workroot / "iso"
    workdir.mkdir(parents=True, exist_ok=True)
    backend, plan = build(workdir)
    from tuner.dsp import snapshot as snap_mod

    shot = snap_mod.capture(backend.device, backend.identity)
    backend.device.arm_writes(
        reason="rehearsal", evidence=shot.save(workdir / "iso.json")
    )

    isolator = MuteIsolator(backend)
    isolator.begin(shot)
    try:
        isolator.prove_silence(lambda: None)
    except IsolationError as exc:
        check("a path mute does not silence is caught", True, str(exc)[:64])
    else:
        check("a path mute does not silence is caught", False, "did not raise")

    isolator = MuteIsolator(backend)
    isolator.begin(shot)
    rig = SyntheticRig(backend)
    isolator.prove_silence(
        lambda: rig.measure(0, backend.stimulus_limit(0), "silence-proof")
    )
    isolator.isolate([3])
    muted = [backend.read_channel(ch).muted for ch in range(8)]
    check(
        "exactly one output is audible",
        muted.count(False) == 1 and muted[3] is False,
    )
    before = backend.device.stats.writes
    isolator.isolate([4])
    check(
        "moving isolation along costs two non-volatile writes",
        backend.device.stats.writes - before == 2,
        f"{backend.device.stats.writes - before} writes",
    )
    isolator.restore()
    check("restore puts every mute state back", snap_matches(backend, shot))

    fake = backend.device.session.transport.device
    for channel in (6, 7):
        record = bytearray(fake.image.channels[channel])
        record[35 * 8 + 7] = 3
        fake.image.channels[channel] = record
    try:
        MuteIsolator(backend).begin(snap_mod.capture(backend.device, backend.identity))
    except IsolationError as exc:
        check("a linked pair is refused, not half-muted", True, str(exc)[:64])
    else:
        check("a linked pair is refused, not half-muted", False, "did not raise")

    try:
        NoIsolation(basis="  ")
    except IsolationError:
        check("declared isolation without a basis is refused", True)
    else:
        check("declared isolation without a basis is refused", False, "did not raise")

    print("\nStage 10 -- gangs: two subwoofers in one ported box")
    workdir = workroot / "gang"
    workdir.mkdir(parents=True, exist_ok=True)
    backend, _ = build(workdir)
    link_in_the_app(backend)
    plan = make_ganged_plan(workdir / "baseline.json")
    rig = SyntheticRig(backend)
    report = TuneRun(
        plan, backend, rig, MuteIsolator(backend, gangs=(SUBS.outputs,))
    ).execute()
    if verbose:
        print("\n" + report.summary() + "\n")

    check(
        "a ganged run is accepted",
        report.outcome is Outcome.ACCEPTED,
        report.error or "",
    )
    check(
        "both subwoofers hold one tune",
        backend.tuning_digest(6) == backend.tuning_digest(7),
    )
    check(
        "the gang is two outputs but one source",
        len(plan.outputs) == 3 and len(plan.sources) == 2,
        f"{len(plan.outputs)} outputs, {len(plan.sources)} sources",
    )
    check(
        "the gang was swept as one, never a member alone",
        all(output in (0, 6) for output, _, _ in rig.calls),
    )
    check(
        "writes to linked outputs transmitted, because the gang acknowledged them",
        report.stage_data("write", "block_writes") > 0,
    )

    workdir = workroot / "gang-split"
    workdir.mkdir(parents=True, exist_ok=True)
    backend, _ = build(workdir)
    _snapshot(backend, workdir, "pre.json")
    backend.write_channel(7, replace(backend.read_channel(7), gain_dbfs=-13.0))
    backend.device.disarm()
    report = TuneRun(
        make_ganged_plan(workdir / "baseline.json"),
        backend,
        SyntheticRig(backend),
        MuteIsolator(backend, gangs=(SUBS.outputs,)),
    ).execute()
    check(
        "a gang mismatched before the run stops it, before any sweep",
        "before any write" in (report.error or ""),
        (report.error or "")[:70],
    )

    try:
        Gang(outputs=(6, 7))
    except PlanError:
        check("a gang without a basis is refused", True)
    else:
        check("a gang without a basis is refused", False, "did not raise")

    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    print(f"workdir: {workroot}")
    if failed:
        print("\nFAILED:")
        for name, _, detail in failed:
            print(f"  {name} -- {detail}")
        return 1
    print(
        "\nEvery outcome the run can produce was reached: accepted, rejected,\n"
        "indeterminate twice over, aborted, and a rollback that failed its\n"
        "acoustic check. Nothing touched a real device."
    )
    return 0


def _snapshot(backend, workdir: Path, name: str = "iso.json"):
    """Capture, save and arm. Returns the evidence, which is what arming took."""
    from tuner.dsp import snapshot as snap

    shot = snap.capture(backend.device, backend.identity)
    evidence = shot.save(workdir / name)
    backend.device.arm_writes(reason="rehearsal", evidence=evidence)
    return evidence


def snap_matches(backend, shot) -> bool:
    from tuner.dsp import snapshot as snap

    return snap.compare(backend.device, shot) == {}


def _leading_run(workdir: Path):
    workdir.mkdir(parents=True, exist_ok=True)
    fake = FakeDsp408()
    session = Dsp408Session(
        LoopbackTransport(fake),
        policy=TxPolicy(allow_writes=True, allow_presets=True),
        pacing=Pacing(idle_after_reply_s=0.0, max_requests_per_s=1e9),
    )
    device = Dsp408Device(session, session_id="rehearsal")
    backend = Dsp408Spp(device=device, peq_policy=PeqPolicy.LEADING)
    backend.connect()
    return TuneRun(
        make_plan(workdir / "b.json", 6),
        backend,
        SyntheticRig(backend),
        MuteIsolator(backend),
    )


# -- wiring -----------------------------------------------------------------


# -- the bench session ------------------------------------------------------

#: **The interface's rate is not the DSP's rate.** The ADAU1701 runs at a
#: fixed 48 kHz internally; the Scarlett Solo is configured at 44.1 kHz in
#: Windows, and WASAPI shared mode accepts only the rate the device is set to.
#: Hardcoding 48 kHz here failed with ``Invalid sample rate`` -- so the rate is
#: read from the device rather than assumed, and the two ends are required to
#: agree.
#:
#: ``bench_golden.py`` hardcodes 44100 and works because that is what this
#: interface happens to be set to. Querying is the version that survives
#: someone changing it in the Windows control panel.
#: The Scarlett, by host-API-qualified name. **Never by index**: MME lists the
#: host default first, so indices renumber when it changes, and on this very
#: bench that once pointed a measurement at the PC speakers while still
#: capturing the correct input -- a smooth curve made of noise.
#:
#: And never ``Speakers (DSP-408)``, which appears in the Windows device list
#: whenever our Bluetooth control link is up. It is the DSP's **A2DP sink**:
#: an SBC-compressed lossy path that would wreck a measurement while looking
#: like the obvious device to pick. The standing rule says do not select by
#: index; this is the case where the *name* is the bait.
OUT_DEVICE = "Speakers (Scarlett Solo USB)"
IN_DEVICE = "Microphone (Scarlett Solo USB)"

#: Interface output 0, interface input 1 -- the Solo's instrument input, where
#: ``docs/hardware.md`` puts the DUT. The same two numbers ``bench_golden.py``
#: used for the REW comparison, so this measures the path that was validated.
OUT_CHANNEL = 0
IN_CHANNEL = 1


def _devices(host_api: str) -> tuple[str, str]:
    return (f"{IN_DEVICE}, {host_api}", f"{OUT_DEVICE}, {host_api}")


def _sample_rate(device: tuple[str, str]) -> int:
    """The rate both ends of the interface are configured for.

    Raises if they disagree rather than picking one: a duplex stream needs a
    single rate, and guessing which end to believe would produce a resampled
    capture that looks entirely plausible.
    """
    import sounddevice as sd

    rates = {name: int(sd.query_devices(name)["default_samplerate"]) for name in device}
    if len(set(rates.values())) != 1:
        raise SystemExit(
            f"interface ends disagree on sample rate: {rates}. Set both to the "
            f"same rate in Windows Sound settings before measuring."
        )
    return next(iter(rates.values()))


def _live_backend(args, writable: bool = False):
    """A real ``Dsp408Spp`` over RFCOMM.

    ``writable`` only opens the transmit policy; a write still needs
    ``arm_writes`` with a verified on-disk snapshot. Two keys, not one.
    """
    from tuner.dsp.transport import RfcommSocketTransport, SerialPortTransport

    if args.port:
        transport = SerialPortTransport(args.port)
        name = f"serial({args.port})"
    elif args.address:
        transport = RfcommSocketTransport(args.address, args.channel)
        name = f"rfcomm({args.address}:{args.channel})"
    else:
        raise SystemExit("give --address or --port")

    policy = TxPolicy(
        allow_writes=writable,
        # A tuning run stores its baseline to a preset slot at ARM, so presets
        # are permitted whenever writes are. They stay a separate switch
        # because a recall replaces all eight channels at once.
        allow_presets=writable,
        blast_radius=BlastRadius(
            max_writes=getattr(args, "max_writes", 64),
            max_channels=getattr(args, "max_channels", 1),
        ),
    )
    session = Dsp408Session(transport, policy=policy, bluetooth_device_id=args.link_id)
    journal = WriteJournal(
        Path(args.journal) if getattr(args, "journal", None) else None
    )
    device = Dsp408Device(session, journal=journal, session_id="bench")
    return session, Dsp408Spp(device=device, peq_policy=PeqPolicy.EXCLUSIVE), name


def cmd_measure(args) -> int:
    """Establish this session's measurement floor, with the DSP in circuit.

    Everything downstream depends on these two numbers, and neither survives
    between sessions:

    * **Level linearity.** A compressor, limiter, gate or AGC anywhere in the
      chain invalidates single-level measurement entirely. Not automatic
      inside ``capture_sweep`` because it costs ~14 s; a run that skips it is
      not verified whatever the curve looks like.
    * **The repeatability floor.** The improvement invariant's threshold is
      this spread, not zero. It moves with temperature, mounting and ambient
      noise, so it is measured per session and never inherited from the last.

    Read-only with respect to the DSP: the channel is read so a stimulus
    ceiling can be derived from live device state, and nothing is written.
    """
    from tuner.measure.capture import CaptureConfig, SessionInfo, capture_sweep
    from tuner.measure.metrics import log_freqs
    from tuner.measure.qa import measure_level_linearity, require_linear_path
    from tuner.optimize.target import from_points
    from tuner.optimize.verify import measure_repeatability
    from tuner.orchestrate.objective import MagnitudeObjective

    device = _devices(args.host_api)
    sample_rate_hz = _sample_rate(device)
    output = args.output - 1

    session_dsp, backend, transport = _live_backend(args)
    with session_dsp:
        session_dsp.handshake()
        live = backend.read_channel(output)
        limit = backend.stimulus_limit(output)

    print(f"DSP            {transport}")
    print(
        f"interface      {sample_rate_hz} Hz "
        f"(the DSP runs 48 kHz internally; these are different things)"
    )
    print(f"OUT{args.output} gain      {live.gain_dbfs:+.1f} dB")
    print(
        f"OUT{args.output} passband  "
        f"{live.crossover.high_pass_hz:.0f} - {live.crossover.low_pass_hz:.0f} Hz"
    )
    print(
        f"stimulus limit {limit.ceiling_dbfs:+.1f} dBFS  "
        f"(characterized={limit.characterized})"
    )
    print(f"sweep level    {args.level_dbfs:+.1f} dBFS")
    if args.level_dbfs > limit.ceiling_dbfs:
        print(
            f"\nREFUSED: {args.level_dbfs:+.1f} dBFS is above the ceiling the "
            f"device's own gain and EQ leave ({limit.ceiling_dbfs:+.1f} dBFS). "
            f"Hard safety rule 6 -- the DSP's gain is downstream of the "
            f"limiter, so it is subtracted here rather than hoped about."
        )
        return 2

    # Tones inside this channel's passband. The module default is
    # 300/1000/3000 Hz and 300 Hz sits in OUT1's stopband, where a level sweep
    # measures the high-pass skirt rather than the path's linearity.
    tones = _passband_tones(live)
    print(f"\nLevel linearity at {tones} Hz (~14 s) ...")
    linearity = measure_level_linearity(
        sample_rate_hz=sample_rate_hz,
        output_channel=OUT_CHANNEL,
        input_channel=IN_CHANNEL,
        device=device,
        limit=limit,
        freqs_hz=tones,
    )
    require_linear_path(linearity)
    print(f"  gain spread {linearity.spread_db:.2f} dB across level -- linear.")

    capture = CaptureConfig(
        sample_rate_hz=sample_rate_hz,
        device=device,
        output_channel=OUT_CHANNEL,
        input_channels=(IN_CHANNEL,),
        level_dbfs=args.level_dbfs,
        limit=limit,
        repeats=args.repeats,
    )
    info = SessionInfo(
        gains_db=(0.0,),
        temperature_c=args.temperature_c,
        coupling=Coupling.ELECTRICAL,
        setup_token=args.setup_token,
        notes={"purpose": "bench session floor", "dsp_output": str(args.output)},
    )

    # Score inside the passband only. Outside it the channel is 24 dB/octave
    # of stopband, where there is no signal and the objective would be
    # scoring the shape of the noise floor.
    lo, hi = _score_band(live)
    freqs = log_freqs(lo, hi, args.points)
    print(f"\nScoring band   {lo:.0f} - {hi:.0f} Hz, {args.points} log points")

    print(f"\n{args.trials} identical sweeps, nothing touched between ...")
    if args.spacing_s:
        print(
            f"  spaced {args.spacing_s:.0f} s apart, so the spread covers "
            f"drift and not only short-term noise"
        )
    curves = []
    started_s = time.monotonic()
    for i in range(args.trials):
        if i and args.spacing_s:
            time.sleep(args.spacing_s)
        curves.append(capture_sweep(capture, info)[IN_CHANNEL])
        print(f"  {i + 1}/{args.trials}")
    span_s = time.monotonic() - started_s

    # How hard the converter was driven. Printed because setting input gain is
    # otherwise guesswork, and a session that discovers it had 1 dB of
    # headroom discovers it by losing a sweep.
    _report_headroom(capture, info)

    # The target is the first sweep's own magnitude, so the score of a repeat
    # is literally "how far did this drift from the first". Level-matched
    # inside the objective, which is what makes it a shape comparison.
    first_db = curves[0].magnitude_dbfs(freqs)
    objective = MagnitudeObjective(
        name="repeatability: first sweep as target",
        target=from_points(
            list(zip(freqs.tolist(), first_db.tolist(), strict=True)), "first sweep"
        ),
        freqs_hz=freqs,
        source_weights={output: 1.0},
        band_hz=(lo, hi),
        level_band_hz=(lo, hi),
    )
    scores = [objective.score_one(m) for m in curves]
    floor = measure_repeatability(scores, session_id="bench", span_s=span_s)

    print("\nscores (rms dB from the first sweep, level-matched):")
    for i, value in enumerate(scores):
        print(f"  {i + 1}  {value:.4f}")
    print(
        f"\nREPEATABILITY FLOOR  {floor.value:.4f} dB over {floor.n_repeats} "
        f"sweeps spanning {span_s:.0f} s"
    )
    print(
        "\nThat is the improvement invariant's threshold for this session.\n"
        "An accepted tune must beat it. Anything smaller is noise, and\n"
        "accepting it is how a tune accumulates changes that do nothing."
    )
    print(
        "\nThe span is part of the number. Repeats taken back to back measure\n"
        "short-term noise and are blind to drift, and the invariant compares\n"
        "measurements minutes apart -- which makes acceptance too easy and\n"
        "rollback verification too strict, from one figure. If this run took\n"
        "much less than a tuning run will, re-run it with --spacing-s."
    )
    return 0


def _report_headroom(capture, info) -> None:
    """Measure the input peak on a short probe and say what it means.

    Setting input gain is otherwise guesswork, and a session that discovers
    it had 1 dB of headroom discovers it by losing a sweep. In a car a lost
    sweep costs a seat position.
    """
    from tuner.audio.io import play_record
    from tuner.measure.sweep import log_sweep
    from tuner.safety.limits import apply, inspect_capture

    probe = log_sweep(200.0, 8_000.0, 0.3, capture.sample_rate_hz)
    stimulus = apply(probe.samples, capture.level_dbfs, capture.limit)
    recorded = play_record(
        stimulus,
        output_channel=capture.output_channel,
        input_channels=list(capture.input_channels),
        sample_rate_hz=capture.sample_rate_hz,
        device=capture.device,
        tail_s=0.1,
    )
    level = inspect_capture(recorded[:, 0])
    print()
    print(f"Input level    {level.summary()}")
    if level.headroom_db < 6.0:
        print("  TIGHT. Under 6 dB leaves nothing for a louder passage or a")
        print("  warmer amplifier. Back the interface input gain off before")
        print("  measuring anything you intend to keep.")
    elif level.headroom_db > 30.0:
        print("  LOW. Over 30 dB of headroom throws away converter range,")
        print("  which shows up as a higher noise floor and therefore a")
        print("  coarser repeatability floor. Raise the interface input gain.")


def _passband_tones(config) -> tuple[float, ...]:
    """Three tones inside this channel's passband, clear of the corners.

    A tone in the stopband measures the crossover skirt rather than the path,
    which is how a perfectly linear rig gets reported as indeterminate. Listed
    in ``CLAUDE.md`` as a desk cleanup; this is it, for the run's own channel.
    """
    lo = (config.crossover.high_pass_hz or 20.0) * 1.6
    hi = (config.crossover.low_pass_hz or 20_000.0) / 1.6
    if hi <= lo:
        raise SystemExit(
            f"passband {lo:.0f}-{hi:.0f} Hz is too narrow for linearity tones"
        )
    return (round(lo, 1), round((lo * hi) ** 0.5, 1), round(hi, 1))


def _score_band(config) -> tuple[float, float]:
    """Where this channel actually has signal, corners included."""
    return (
        float(config.crossover.high_pass_hz or 20.0),
        float(config.crossover.low_pass_hz or 20_000.0),
    )


#: The ADAU1701's fixed internal rate. Biquad response warps near Nyquist, so
#: a prediction must be evaluated at the rate the filter actually runs at --
#: which is the DSP's, never the interface's. Those differ on this bench
#: (48 000 vs 44 100) and conflating them is a silent error at high frequency.
DSP_RATE_HZ = 48_000


def cmd_predict_check(args) -> int:
    """Write one known EQ band through our own backend and measure the result.

    The decisive experiment before any optimizer runs. Everything downstream
    assumes that a ``Biquad`` the fitter chose, translated by
    ``_band_to_eq``, encoded by ``protocol``, written over RFCOMM and executed
    by the ADAU, produces the response ``biquad.response_db`` predicted. If
    that chain is wrong anywhere, the optimizer converges beautifully against
    a model of a system it has stopped resembling -- the default failure mode
    of this whole class of tool.

    **Differential, per CLAUDE.md.** Two sweeps, band flat then band set, and
    the difference taken. A single sweep would force the comparison to absorb
    the interface, the cabling and the channel's own crossover into one offset
    term, which only works if all of them are flat. Dividing one sweep by the
    other cancels every one of them exactly.

    **A cut, not a boost, and that is deliberate.** A boost reduces the
    headroom the safety limiter has to work with -- hard safety rule 6 -- so
    the second sweep would have to run at a lower level than the first, and
    the differential would then be measuring the level change as well as the
    filter. A cut leaves the ceiling where it was and keeps both sweeps
    identical in every respect but the band under test.
    """
    import numpy as np

    from tuner.dsp import snapshot as snap
    from tuner.dsp.backend import Biquad, FilterType
    from tuner.dsp.dsp408_spp import _band_to_eq, _eq_to_band
    from tuner.dsp.protocol import EqBand
    from tuner.measure.capture import CaptureConfig, SessionInfo, capture_sweep
    from tuner.measure.metrics import log_freqs
    from tuner.optimize import biquad as biquad_mod

    if args.band_db > 0:
        raise SystemExit(
            "predict-check uses a cut. A boost reduces the stimulus ceiling "
            "between the two sweeps and the differential would then include "
            "the level change; see the docstring."
        )

    device = _devices(args.host_api)
    sample_rate_hz = _sample_rate(device)
    output = args.output - 1

    session_dsp, backend, transport = _live_backend(args, writable=True)
    rc = 0
    with session_dsp:
        identity = session_dsp.handshake()
        dev = backend.device
        live = backend.read_channel(output)
        limit = backend.stimulus_limit(output)

        shot = snap.capture(
            dev, identity, transport_name=transport, notes={"stage": "predict-check"}
        )
        evidence = shot.save(Path(args.snapshot_out))

        lo, hi = _score_band(live)
        freqs = log_freqs(lo, hi, args.points)
        want = Biquad(
            freq_hz=args.band_hz,
            gain_dbfs=args.band_db,
            q=args.band_q,
            kind=FilterType.PEAKING,
        )

        print(f"DSP            {transport}")
        print(f"interface      {sample_rate_hz} Hz   DSP {DSP_RATE_HZ} Hz")
        print(f"restore point  {args.snapshot_out}  ({evidence.digest[:16]})")
        print(f"OUT{args.output} passband  {lo:.0f} - {hi:.0f} Hz")
        print(
            f"stimulus limit {limit.ceiling_dbfs:+.1f} dBFS  "
            f"(characterized={limit.characterized})"
        )
        print(f"sweep level    {args.level_dbfs:+.1f} dBFS")
        if args.level_dbfs > limit.ceiling_dbfs:
            print("\nREFUSED: above the ceiling the device's gain and EQ leave.")
            return 2

        stored = EqBand.decode(dev.block(output, args.band))
        if stored.level != 600:
            print(
                f"\nREFUSED: OUT{args.output} band {args.band + 1} is not flat "
                f"(level {stored.level}). Pick a free band with --band; this "
                f"command will not overwrite a band the operator set."
            )
            return 2

        encoded = _band_to_eq(want, stored)
        achieved = _eq_to_band(encoded)
        print(
            f"\nband {args.band + 1}: {want.freq_hz:.0f} Hz, "
            f"{want.gain_dbfs:+.1f} dB, Q {want.q:.2f}"
        )
        print(
            f"  as encoded   freq {encoded.freq} raw, level {encoded.level}, "
            f"bw {encoded.bw}"
        )
        print(
            f"  as achieved  {achieved.freq_hz:.0f} Hz, "
            f"{achieved.gain_dbfs:+.2f} dB, Q {achieved.q:.3f}"
        )
        print("  (quantisation happens here, so the prediction uses ACHIEVED)")

        capture = CaptureConfig(
            sample_rate_hz=sample_rate_hz,
            device=device,
            output_channel=OUT_CHANNEL,
            input_channels=(IN_CHANNEL,),
            level_dbfs=args.level_dbfs,
            limit=limit,
            repeats=args.repeats,
        )
        info = SessionInfo(
            gains_db=(0.0,),
            temperature_c=args.temperature_c,
            # DSP RCA out -> interface line in. No air, no room, no
            # microphone, so temperature is not a term in comparability
            # and recording one would assert a relevance it does not have.
            coupling=Coupling.ELECTRICAL,
            setup_token=args.setup_token,
            notes={"purpose": "predict-check", "dsp_output": str(args.output)},
        )

        print("\nsweep 1 of 2: band flat ...")
        before = capture_sweep(capture, info)[IN_CHANNEL].magnitude_dbfs(freqs)

        if not args.apply:
            print("\nDry run: the band was never written. Re-run with --apply.")
            return 0

        try:
            dev.arm_writes("predict-check: one known band", evidence)
            dev.write_block(
                output, args.band, encoded.encode(), reason="predict-check band"
            )
            print("band written and verified by readback.")

            # Rule 6 again: the ceiling is a function of device state, and we
            # just changed device state. Re-read rather than reuse.
            after_limit = backend.stimulus_limit(output)
            print(f"stimulus limit now {after_limit.ceiling_dbfs:+.1f} dBFS")
            if args.level_dbfs > after_limit.ceiling_dbfs:
                print("\nREFUSED: the written band lowered the ceiling below the")
                print("sweep level. Rolling back without measuring.")
                rc = 2
            else:
                print("\nsweep 2 of 2: band set ...")
                after = capture_sweep(capture, info)[IN_CHANNEL].magnitude_dbfs(freqs)

                measured = after - before
                predicted = biquad_mod.response_db((achieved,), freqs, DSP_RATE_HZ)
                error = measured - predicted

                # **Do not call mean(error) "level drift".** It is only that
                # when the prediction is flat. With a notch in the prediction
                # and nothing in the measurement, mean(error) simply returns
                # -mean(predicted) -- which on 2026-08-12 read +1.888 dB and
                # was briefly mistaken for the device changing level by 1.9 dB.
                # It was the predicted notch's own mean, to three decimals.
                #
                # Level drift is a property of the two *sweeps*, so measure it
                # where the filter is not: outside two octaves around the band.
                # Two octaves is more than a 3-octave passband can spare, so
                # widen the net until something is outside the filter. Reported
                # as "unavailable" rather than NaN when even that fails.
                for octaves in (2.0, 1.5, 1.0):
                    away = np.abs(np.log2(freqs / achieved.freq_hz)) > octaves
                    if away.any():
                        break
                drift = float(np.mean(measured[away])) if away.any() else None
                offset = float(np.mean(error))

                print("\n-- prediction vs measurement " + "-" * 34)
                print(f"  points            {freqs.size} over {lo:.0f}-{hi:.0f} Hz")
                print(f"  measured depth    {measured.min():+.2f} dB")
                print(f"  predicted depth   {predicted.min():+.2f} dB")
                print(f"  mean measured     {float(np.mean(measured)):+.3f} dB")
                print(f"  mean predicted    {float(np.mean(predicted)):+.3f} dB")
                print(
                    f"  residual mean     {offset:+.3f} dB  "
                    f"(NOT level drift -- see the code comment)"
                )
                shown = f"{drift:+.3f} dB" if drift is not None else "unavailable"
                print(
                    f"  level drift       {shown}  "
                    f"(measured {octaves:.1f}+ octaves from the band)"
                )
                print(f"  max |error|       {np.abs(error).max():.3f} dB")
                print(f"  rms error         {float(np.sqrt(np.mean(error**2))):.3f} dB")
                de_offset = error - offset
                print(
                    f"  rms shape error   "
                    f"{float(np.sqrt(np.mean(de_offset**2))):.3f} dB"
                )
        finally:
            print("\n-- rollback " + "-" * 50)
            report = snap.restore(
                dev, shot, outputs=[output], dry_run=False, reason="predict-check back"
            )
            print(report.summary())
            residual = snap.compare(dev, shot)
            print(f"device vs snapshot: {residual or 'identical'}")
            if residual or not report.clean:
                rc = 1
    return rc


def cmd_loop(args) -> int:
    """The closed loop, electrical, with a known answer.

    First time ``tuner.orchestrate`` drives a real backend and a real
    measurer. Everything underneath it has hardware evidence; this layer has
    none, and it is the largest one left.

    **The known answer, and why this shape.** A target curve cannot be
    invented -- ``harman_in_car`` raises by design for exactly that reason, and
    a flat target across OUT1 would ask the fit to EQ out its own crossover.
    So the target is **the channel's own measured response**, taken before
    anything is changed. Then a deliberate notch is written, and the run is
    required to put the response back.

    That is a sharper test than it looks, because it is precisely the case the
    pre-existing-EQ defect got wrong. The run measures a baseline that contains
    the notch; the fit must recognise the notch as *EQ* rather than as speaker,
    subtract it, and fit nothing much -- leaving the EXCLUSIVE write to delete
    it. A run that double-counted would instead fit a boost to cancel a notch
    the write then removes, and land several dB high. The fixed run should
    return to the reference within the session's repeatability floor.

    Nothing is connected to any output. The perturbation is a **cut**, so the
    stimulus ceiling does not move between the reference and the run.
    """
    from tuner.dsp import snapshot as snap
    from tuner.dsp.backend import Biquad, FilterType
    from tuner.dsp.dsp408_spp import _band_to_eq
    from tuner.dsp.protocol import EqBand
    from tuner.measure.capture import CaptureConfig, SessionInfo, capture_sweep
    from tuner.measure.metrics import log_freqs
    from tuner.measure.qa import measure_level_linearity, require_linear_path
    from tuner.optimize.target import from_points
    from tuner.orchestrate.rig import AcousticMeasurer

    device_pair = _devices(args.host_api)
    rate = _sample_rate(device_pair)
    output = args.output - 1

    session, backend, transport = _live_backend(args, writable=True)
    rc = 0
    # **Connect through the backend, not the session.** `Dsp408Spp.identity` is
    # populated by its own `connect()`, and `TuneRun` reads it at ARM for
    # provenance. Opening the session and calling `handshake()` directly leaves
    # the backend not knowing who it is talking to, and the run aborts with
    # "not connected" after the reference sweep -- which is how this was found.
    backend.connect()
    try:
        identity = backend.identity
        dev = backend.device
        live = backend.read_channel(output)
        # Hard safety rule 4: raising a ceiling is a deliberate act that
        # requires knowing what is connected, and the basis is recorded in
        # provenance so a ceiling that turns out wrong is traceable to the
        # claim that set it.
        if args.driver_ceiling_dbfs != DEFAULT_CEILING_DBFS and not args.ceiling_basis:
            raise SystemExit(
                "--driver-ceiling-dbfs above the default needs --ceiling-basis "
                "naming what is connected to this output"
            )
        ceiling_basis = args.ceiling_basis or (
            "conservative default; nothing has been claimed about this output"
        )
        limit = backend.stimulus_limit(output, args.driver_ceiling_dbfs)
        lo, hi = _score_band(live)
        freqs = log_freqs(lo, hi, args.points)

        print(f"DSP            {transport}")
        print(f"interface      {rate} Hz")
        print(
            f"OUT{args.output}           {lo:.0f}-{hi:.0f} Hz, "
            f"{live.gain_dbfs:+.1f} dB, {len(live.peq)} non-flat band(s)"
        )
        print(
            f"stimulus limit {limit.ceiling_dbfs:+.1f} dBFS   "
            f"driver ceiling {args.driver_ceiling_dbfs:+.1f} dBFS"
        )
        print(f"ceiling basis  {ceiling_basis}")

        # The mixer says which outputs this stimulus can actually reach.
        # Decoded 2026-08-12; before that this was a claim in a document.
        if not backend.input_mix(output).reaches(args.input - 1):
            print(
                f"\nREFUSED: OUT{args.output} takes nothing from IN{args.input} "
                f"(mixer reads {backend.input_mix(output).inputs}). The sweep "
                f"would measure silence and be rejected as a dead path."
            )
            return 2

        if args.slot == identity.current_preset:
            print(
                f"\nREFUSED: slot {args.slot} is the preset the device is "
                f"running from. Recall one of your own presets first."
            )
            return 2

        # **Two snapshots, two paths.** The run captures its own at ARM and
        # writes it to ``plan.snapshot_path``; ours is the *pre-perturbation*
        # state, which the run knows nothing about because the perturbation is
        # written after we take it. Passing one path for both let ARM overwrite
        # our restore point, so the teardown faithfully "restored" the device
        # to the perturbed state and left the notch on it. Found on the bench,
        # on a channel with nothing connected.
        out = Path(args.snapshot_out)
        pre = out.with_name(out.stem + "-pre" + out.suffix)
        shot = snap.capture(
            dev,
            identity,
            transport_name=transport,
            notes={"stage": "electrical-loop, pre-perturbation"},
        )
        evidence = shot.save(pre)
        print(f"our restore    {pre.name}  ({evidence.digest[:16]})")
        print(f"run's snapshot {out.name}  (the run writes this at ARM)")

        tones = _passband_tones(live)
        print(f"\nLevel linearity at {tones} Hz (~14 s) ...")
        linearity = measure_level_linearity(
            sample_rate_hz=rate,
            output_channel=OUT_CHANNEL,
            input_channel=IN_CHANNEL,
            device=device_pair,
            limit=limit,
            freqs_hz=tones,
        )
        require_linear_path(linearity)
        print(f"  gain spread {linearity.spread_db:.2f} dB -- linear.")

        capture = CaptureConfig(
            sample_rate_hz=rate,
            device=device_pair,
            output_channel=OUT_CHANNEL,
            input_channels=(IN_CHANNEL,),
            level_dbfs=limit.ceiling_dbfs - args.headroom_db,
            limit=limit,
            repeats=args.repeats,
        )
        info = SessionInfo(
            gains_db=(0.0,),
            temperature_c=args.temperature_c,
            # DSP RCA out -> interface line in. No air, no room, no
            # microphone, so temperature is not a term in comparability
            # and recording one would assert a relevance it does not have.
            coupling=Coupling.ELECTRICAL,
            setup_token=args.setup_token,
            notes={"purpose": "electrical closed loop"},
        )

        print("\n-- reference sweep (this becomes the target) -----------")
        reference = capture_sweep(capture, info)[IN_CHANNEL].magnitude_dbfs(freqs)
        print(f"  {freqs.size} points over {lo:.0f}-{hi:.0f} Hz")

        objective = MagnitudeObjective(
            name=f"electrical loop: OUT{args.output}'s own pre-perturbation response",
            target=from_points(
                list(zip(freqs.tolist(), reference.tolist(), strict=True)),
                "measured reference",
            ),
            freqs_hz=freqs,
            source_weights={output: 1.0},
            band_hz=(lo, hi),
            level_band_hz=(lo, hi),
        )

        plan = TunePlan(
            session_id="electrical-loop-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
            objective=objective,
            scratch_slot=args.slot,
            scratch_slot_confirmed_by=args.confirmed_by,
            scratch_slot_holds="the tuner's own baseline from a previous run",
            snapshot_path=out,
            constraints=biquad.DEFAULT_CONSTRAINTS,
            driver_ceilings={
                output: DriverCeiling(args.driver_ceiling_dbfs, ceiling_basis)
            },
            floor_repeats=args.floor_repeats,
            notes={"rig": "Scarlett Solo, electrical", "purpose": "known answer"},
        )

        print("\n-- writing the perturbation ---------------------------")
        dev.arm_writes("electrical loop: deliberate perturbation", evidence)
        stored = EqBand.decode(dev.block(output, args.band))
        if stored.level != 600:
            print(f"REFUSED: band {args.band + 1} is not flat. Pick a free one.")
            return 2
        want = Biquad(
            freq_hz=args.band_hz,
            gain_dbfs=args.band_db,
            q=args.band_q,
            kind=FilterType.PEAKING,
        )
        dev.write_block(
            output,
            args.band,
            _band_to_eq(want, stored).encode(),
            reason="electrical loop perturbation",
        )
        print(
            f"  band {args.band + 1}: {args.band_hz:.0f} Hz, "
            f"{args.band_db:+.1f} dB, Q {args.band_q:.2f} -- written, verified"
        )

        measurer = AcousticMeasurer(
            config=capture,
            session=info,
            positions=("bench",),
            linearity=linearity,
            headroom_db=args.headroom_db,
        )
        isolator = NoIsolation(
            f"bench, electrical: only OUT{args.output} is cabled, to the "
            f"interface's line input. Nothing is connected to any other "
            f"output, so there is no path a sweep could confirm."
        )

        print("\n-- the run --------------------------------------------")
        run = TuneRun(plan, backend, measurer, isolator)
        report = None
        failure = None
        try:
            report = run.execute()
        except Exception as exc:  # noqa: BLE001 - reported below, then returned
            # **Print the stages before anything else.** A run that raises is
            # exactly when its stage log is most wanted, and the first version
            # of this command let the traceback swallow it -- leaving a
            # RollbackFailed message with no way to see what the fit had done.
            failure = exc
        finally:
            print("\n-- restoring the device -------------------------------")
            # **Re-arm.** SETTLE disarms writes when it accepts, so a teardown
            # that assumes the run left it armed raises WritesNotArmed -- after
            # the run, with our perturbation still on the device. Re-verifying
            # the evidence here is exactly what arming is for.
            dev.arm_writes("loop teardown", evidence)
            back = snap.restore(dev, shot, dry_run=False, reason="loop teardown")
            print(back.summary().splitlines()[0])
            residual = snap.compare(dev, shot)
            print(f"device vs snapshot: {residual or 'identical'}")
            if residual:
                rc = 1

        print()
        stages = report.stages if report is not None else tuple(run._stages)
        for record in stages:
            mark = "ok  " if record.ok else "FAIL"
            print(f"  [{mark}] {record.stage.value:<9} {record.detail}")

        if report is None:
            print(f"\nthe run raised {type(failure).__name__}:")
            print(f"  {failure}")
            return 1

        verdict = report.verdict
        print()
        if verdict is None:
            print("no verdict -- the run aborted. See the stages above.")
            return 1
        print(f"outcome        {verdict.outcome.value}")
        print(f"baseline score {verdict.baseline_score:.4f} dB")
        print(f"result score   {verdict.result_score:.4f} dB")
        print(f"floor          {float(verdict.floor):.4f} dB")
        print(f"reason         {verdict.reason}")

        # The known answer: the run should have put the response back where it
        # started. The score is against the reference, so a good result is a
        # score near zero rather than an improvement over some other curve.
        print()
        if verdict.result_score < verdict.baseline_score:
            print("The run improved on the perturbed baseline, which is the")
            print("least it had to do. What matters more is how close to the")
            print("reference it landed -- see 'result score', which is rms")
            print("deviation from the pre-perturbation response.")
    finally:
        backend.disconnect()
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "measure",
        help="establish this session's linearity and repeatability floor",
    )
    p.add_argument("--address", help="Bluetooth address of the DSP-408")
    p.add_argument("--port", help="Bluetooth SPP COM port")
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--link-id", type=int, default=4)
    p.add_argument("--output", type=int, default=1, help="1-based DSP output")
    p.add_argument("--level-dbfs", type=float, default=-12.0)
    p.add_argument(
        "--setup-token",
        help=(
            "the operator's verbatim claim about the physical setup, recorded "
            "in provenance and compared literally between measurements. "
            "Optional on an electrical bench; REQUIRED once a microphone is "
            "involved, where mic position, seat, doors, HVAC and occupancy "
            "move the response far more than temperature and none of them is "
            "visible to any code."
        ),
    )
    p.add_argument("--repeats", type=int, default=3, help="passes per sweep, medianed")
    p.add_argument("--trials", type=int, default=3, help="sweeps for the floor")
    p.add_argument(
        "--spacing-s",
        type=float,
        default=0.0,
        help=(
            "idle seconds between floor sweeps. The floor is used to judge "
            "measurements taken minutes apart, so back-to-back repeats "
            "understate it -- spread them over the time a tuning run takes."
        ),
    )
    p.add_argument("--points", type=int, default=300)
    p.add_argument("--temperature-c", type=float, default=None)
    p.add_argument("--host-api", default="Windows WASAPI")
    p.set_defaults(func=cmd_measure)

    p = sub.add_parser(
        "predict-check",
        help="write one known EQ band and compare the measured result to the model",
    )
    p.add_argument("--address", help="Bluetooth address of the DSP-408")
    p.add_argument("--port", help="Bluetooth SPP COM port")
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--link-id", type=int, default=4)
    p.add_argument("--journal")
    p.add_argument("--apply", action="store_true", help="actually write the band")
    p.add_argument("--snapshot-out", required=True)
    p.add_argument("--output", type=int, default=1, help="1-based DSP output")
    p.add_argument("--band", type=int, default=0, help="0-based EQ band index")
    p.add_argument("--band-hz", type=float, default=1000.0)
    p.add_argument("--band-db", type=float, default=-6.0, help="a cut; see the docs")
    p.add_argument("--band-q", type=float, default=2.0)
    p.add_argument("--level-dbfs", type=float, default=-20.0)
    p.add_argument(
        "--setup-token",
        help=(
            "the operator's verbatim claim about the physical setup, recorded "
            "in provenance and compared literally between measurements. "
            "Optional on an electrical bench; REQUIRED once a microphone is "
            "involved, where mic position, seat, doors, HVAC and occupancy "
            "move the response far more than temperature and none of them is "
            "visible to any code."
        ),
    )
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--points", type=int, default=300)
    p.add_argument("--temperature-c", type=float, default=None)
    p.add_argument("--host-api", default="Windows WASAPI")
    p.set_defaults(func=cmd_predict_check)

    p = sub.add_parser(
        "loop",
        help="the electrical closed loop, known-answer. Writes to the device.",
    )
    p.add_argument("--address", help="Bluetooth address of the DSP-408")
    p.add_argument("--port", help="Bluetooth SPP COM port")
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--link-id", type=int, default=4)
    p.add_argument("--journal")
    p.add_argument("--snapshot-out", required=True)
    p.add_argument("--slot", type=int, required=True, help="scratch preset slot")
    p.add_argument("--confirmed-by", required=True)
    p.add_argument("--output", type=int, default=1, help="1-based DSP output")
    p.add_argument("--input", type=int, default=1, help="1-based DSP input driven")
    p.add_argument("--band", type=int, default=0, help="0-based slot for the notch")
    p.add_argument("--band-hz", type=float, default=1200.0)
    p.add_argument("--band-db", type=float, default=-6.0, help="a cut, not a boost")
    p.add_argument("--band-q", type=float, default=2.0)
    p.add_argument(
        "--driver-ceiling-dbfs",
        type=float,
        default=DEFAULT_CEILING_DBFS,
        help="per hard safety rule 4. Above the default needs --ceiling-basis.",
    )
    p.add_argument(
        "--ceiling-basis",
        help="what is connected to this output, recorded in provenance",
    )
    p.add_argument("--headroom-db", type=float, default=3.0)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--floor-repeats", type=int, default=3)
    p.add_argument("--points", type=int, default=300)
    p.add_argument("--temperature-c", type=float, default=None)
    p.add_argument(
        "--setup-token",
        help=(
            "the operator's verbatim claim about the physical setup, recorded "
            "in provenance and compared literally between measurements. "
            "Optional on an electrical bench; REQUIRED once a microphone is "
            "involved, where mic position, seat, doors, HVAC and occupancy "
            "move the response far more than temperature and none of them is "
            "visible to any code."
        ),
    )
    p.add_argument("--host-api", default="Windows WASAPI")
    p.set_defaults(func=cmd_loop, apply=True, max_writes=400, max_channels=8)

    p = sub.add_parser("rehearse", help="run every outcome against the fake")
    p.add_argument("--verbose", action="store_true", help="print each run's report")

    p = sub.add_parser("plan", help="print a plan's fingerprint and canonical form")
    p.add_argument("--slot", type=int, required=True, help="scratch preset slot")
    p.add_argument(
        "--confirmed-by",
        required=True,
        help="who confirmed the slot is expendable, recorded verbatim",
    )
    p.add_argument("--snapshot", default="baseline.json")

    args = ap.parse_args()
    # Dispatch on the handler the subparser set, when it set one. The older
    # two commands predate that convention and are matched by name.
    if getattr(args, "func", None) is not None:
        return args.func(args)
    if args.command == "rehearse":
        return rehearse(args.verbose)

    plan = make_plan(Path(args.snapshot), args.slot, args.confirmed_by)
    import json

    print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
    print(f"\nplan fingerprint:      {plan.fingerprint()}")
    print(f"objective fingerprint: {plan.objective.fingerprint()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
