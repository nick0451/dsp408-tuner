"""Talk to a DSP-408: enumerate, snapshot, compare, restore, reconcile.

Read-only unless you pass ``--apply``, which only ``restore``, ``noop-write``,
``stage5`` and ``stage6`` accept. Every other subcommand is incapable of writing.

The bring-up stages have their own subcommands, because none was reachable
through the general ones:

    idle        Stage 2 -- connect, send nothing, time the drop
    noop-write  Stage 4 -- send a block's own bytes back to the device
    stage5      Stage 5 -- one real write, verified, then rolled back
    stage6      Stage 6 -- multi-block, multi-channel, and the gang

``stage5`` is hard-wired to the one transition the capture contains and
refuses any other starting state. There is deliberately **no** general
set-a-parameter command here: this tool should not own the ability to write
arbitrary bytes to arbitrary blocks until the narrow case is proven.

**Rehearse against the fake first.** ``--fake`` runs the entire flow in
process, with no hardware and no Bluetooth, against a device that holds real
state and refuses to improvise. The bench should not be the first time any of
this executes::

    python tools/dsp408_probe.py --fake enumerate
    python tools/dsp408_probe.py --fake snapshot -o /tmp/base.json
    python tools/dsp408_probe.py --fake verify /tmp/base.json
    python tools/dsp408_probe.py --fake restore /tmp/base.json          # dry run
    python tools/dsp408_probe.py --fake restore /tmp/base.json --apply

**Seed the fake from a real snapshot when the stage branches on device state.**
``DeviceImage.flat()`` has no link group and no non-flat EQ bands, so a gang or
multi-block test finds nothing to do and says so as a pass::

    python tools/dsp408_probe.py --fake --fake-from snapshots/<date>.json \
        stage6 --snapshot-out /tmp/s6.json --apply

Against real hardware, pair the device first, then::

    python tools/dsp408_probe.py --address 00:13:EF:A0:09:10 enumerate
    python tools/dsp408_probe.py --port COM7 snapshot -o snapshots/base.json

**Run against a real DSP-408 on 2026-08-11**, through Stage 6. Reads: 31/31
transactions, firmware ``MYDW-AV1.06``, zero framing resyncs, link survives
120 s of silence. Writes: the Stage 4 no-op; Stage 5's real change with a
verified rollback; and Stage 6's 46 writes across four channels, including the
backend's own multi-block ``write_channel`` and a gang write read back holding
one tune. The device was byte-identical to a morning snapshot after every one.
**Still unproven on hardware: the preset-recall restore path, all eight
channels in one run, and writing while audio plays.**
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tuner.dsp import ddp  # noqa: E402
from tuner.dsp import snapshot as snap
from tuner.dsp.backend import ChannelConfig, Crossover  # noqa: E402
from tuner.dsp.device import (  # noqa: E402
    Dsp408Device,
    UnsendablePlan,
    WriteJournal,
    describe_diff,
)
from tuner.dsp.dsp408_spp import (  # noqa: E402
    ADDRESSABLE_BANDS,
    FLAT_LEVEL_RAW,
    Dsp408Spp,
    PeqPolicy,
)
from tuner.dsp.fake_device import (
    DeviceImage,  # noqa: E402
    FakeDsp408,  # noqa: E402
)
from tuner.dsp.protocol import EqBand, UnsendableFrame  # noqa: E402
from tuner.dsp.session import (  # noqa: E402
    N_OUTPUTS,
    OBSERVED_BLUETOOTH_DEVICE_ID,
    Dsp408Session,
    Pacing,
)
from tuner.dsp.transport import (  # noqa: E402
    LoopbackTransport,
    RfcommSocketTransport,
    SerialPortTransport,
)
from tuner.dsp.txpolicy import BlastRadius, TxPolicy  # noqa: E402


def _fake_device(args) -> FakeDsp408:
    """The in-process device, optionally wearing the real one's state.

    ``DeviceImage.flat()`` is uniform: identical channels, no link group, no
    non-flat EQ bands. That is right for unit tests and **wrong for a
    rehearsal**, because a bring-up stage that exercises gangs or multi-block
    writes finds nothing to exercise and reports a pass it did not earn. The
    bench would then be the first place those paths ever ran, which is the one
    thing this tool exists to prevent.

    ``--fake-from <snapshot>`` seeds the fake with a real device's records, so
    the rehearsal sees the real link groups, the real band layout and the real
    crossovers. Added 2026-08-11 after a Stage 6 rehearsal "passed" part A by
    writing a single block and skipped part C entirely -- both because the flat
    image had nothing to write, and neither failure was visible as a failure.
    """
    if not args.fake_from:
        return FakeDsp408()
    shot = snap.DeviceSnapshot.load(Path(args.fake_from))
    image = DeviceImage.flat()
    image.channels = [bytearray(r) for r in shot.channels]
    image.preset_names = list(shot.preset_names)
    image.current_preset = shot.current_preset
    image.system = dict(shot.system_blocks)
    return FakeDsp408(image)


def build_session(args) -> tuple[Dsp408Session, str]:
    """A session over whichever transport the flags selected."""
    link = {"bluetooth_device_id": args.link_id}
    if args.fake:
        seeded = f"<-{Path(args.fake_from).name}" if args.fake_from else ""
        return (
            Dsp408Session(
                LoopbackTransport(_fake_device(args)),
                policy=_policy(args),
                pacing=Pacing(idle_after_reply_s=0.0, max_requests_per_s=1e9),
                **link,
            ),
            f"loopback(fake{seeded})",
        )
    if args.port:
        return (
            Dsp408Session(SerialPortTransport(args.port), policy=_policy(args), **link),
            f"serial({args.port})",
        )
    if args.address:
        return (
            Dsp408Session(
                RfcommSocketTransport(args.address, args.channel),
                policy=_policy(args),
                **link,
            ),
            f"rfcomm({args.address}:{args.channel})",
        )
    raise SystemExit("give one of --fake, --port or --address")


def _policy(args) -> TxPolicy:
    # Writes stay off unless the subcommand explicitly turned them on, and the
    # blast radius stays at one channel unless widened on purpose.
    return TxPolicy(
        allow_writes=getattr(args, "apply", False),
        # Separate from allow_writes on purpose: a recall replaces all eight
        # channels at once, which is catastrophic by accident and is exactly
        # what rollback needs on purpose.
        allow_presets=getattr(args, "slot", None) is not None,
        blast_radius=BlastRadius(
            max_writes=getattr(args, "max_writes", 64),
            max_channels=getattr(args, "max_channels", 1),
        ),
    )


def _device(session: Dsp408Session, args) -> Dsp408Device:
    journal = WriteJournal(Path(args.journal) if args.journal else None)
    return Dsp408Device(
        session, journal=journal, session_id=args.session_id or _stamp()
    )


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


# -- subcommands ------------------------------------------------------------


def cmd_enumerate(args) -> int:
    """Connect, run the connect ritual, print what the device says."""
    session, transport = build_session(args)
    with session:
        identity = session.handshake()

    print(f"transport      {transport}")
    print(f"firmware       {identity.firmware}")
    print(f"current preset {identity.current_preset}")
    print(f"link group     {sorted(identity.linked_channels()) or 'none'}")
    print("\npresets")
    for i, name in enumerate(identity.preset_names, start=1):
        mark = " <- current" if i == identity.current_preset else ""
        print(f"  {i:2d}  {name!r}{mark}")

    print("\nsystem blocks (unknown ones printed verbatim, not interpreted)")
    for channel, payload in sorted(identity.system_blocks.items()):
        shown = payload.hex(" ") if payload else "(empty)"
        print(f"  ch{channel:<3d} {shown}")

    print("\nchannels")
    for ch, record in enumerate(identity.channels):
        misc = record[248:256]
        gain = int.from_bytes(misc[2:4], "little")
        delay = int.from_bytes(misc[4:6], "little")
        xover = record[256:264]
        print(
            f"  OUT{ch + 1}  gain_raw {gain:4d}  delay {delay:4d} samp  "
            f"hp {int.from_bytes(xover[0:2], 'little'):5d} Hz  "
            f"lp {int.from_bytes(xover[4:6], 'little'):5d} Hz"
        )
    print(f"\n{session.stats.requests} requests, clean={session.stats.clean}")
    return 0


def cmd_snapshot(args) -> int:
    """Read the whole device and write a restore point."""
    session, transport = build_session(args)
    with session:
        identity = session.handshake()
        device = _device(session, args)
        shot = snap.capture(
            device,
            identity,
            transport_name=transport,
            notes=dict(n.split("=", 1) for n in args.note),
        )

    out = Path(args.out)
    evidence = shot.save(out)
    print(f"wrote {out}")
    print(f"  digest    {shot.digest}")
    print(f"  captured  {shot.provenance.captured_utc}")
    print(f"  firmware  {shot.provenance.firmware}")
    name = shot.preset_names[shot.current_preset - 1]
    print(f"  preset    {shot.current_preset} ({name!r})")
    print(f"  file hash {evidence.digest[:16]}")

    if args.ddp_template:
        template = ddp.parse(Path(args.ddp_template).read_bytes())
        blob = shot.to_ddp(template)
        ddp_out = out.with_suffix(".DDP")
        ddp_out.write_bytes(blob)
        print(f"\nwrote {ddp_out} ({len(blob)} bytes)")
        print("  A restore path through the vendor app, sharing no code with")
        print("  ours. Load it once as a rehearsal before relying on it.")

    print("\nCopy this off the machine. It is the only rollback that exists")
    print("until a preset opcode is known.")
    return 0


def cmd_verify(args) -> int:
    """Compare the live device against a snapshot. Read-only."""
    shot = snap.DeviceSnapshot.load(Path(args.snapshot))
    session, _ = build_session(args)
    with session:
        session.handshake()
        device = _device(session, args)
        drift = snap.compare(device, shot)
        live = {ch: device.record(ch) for ch in range(8)}

    if not drift:
        print(f"device matches {args.snapshot} exactly.")
        return 0

    print(f"device DIFFERS from {args.snapshot}:\n")
    for ch, blocks in sorted(drift.items()):
        print(f"OUT{ch + 1}: {len(blocks)} block(s) differ")
        for line in describe_diff(shot.channels[ch], live[ch]):
            print(line)
    print(
        "\nLeft is the snapshot, right is the device. If you did not make "
        "these\nchanges, suspect a preset recall -- it is the one action that "
        "overwrites\nthe working area."
    )
    return 1


def cmd_diff(args) -> int:
    """Diff two snapshots. Touches no hardware."""
    a = snap.DeviceSnapshot.load(Path(args.before))
    b = snap.DeviceSnapshot.load(Path(args.after))
    if a.digest == b.digest:
        print("identical")
        return 0
    for ch in range(8):
        lines = describe_diff(a.channels[ch], b.channels[ch])
        if lines:
            print(f"OUT{ch + 1}:")
            print("\n".join(lines))
    return 1


def cmd_restore(args) -> int:
    """Put the device back to a snapshot. Dry run unless --apply."""
    shot = snap.DeviceSnapshot.load(Path(args.snapshot))
    session, _ = build_session(args)
    with session:
        identity = session.handshake()
        device = _device(session, args)

        if identity.firmware != shot.provenance.firmware:
            raise SystemExit(
                f"snapshot is from firmware {shot.provenance.firmware!r}, "
                f"device reports {identity.firmware!r}. Refusing."
            )

        if args.apply:
            # Evidence is derived from the file as it stands. Re-saving it to
            # obtain evidence would modify the very thing whose stability is
            # the point.
            device.arm_writes(
                args.reason, snap.DeviceSnapshot.evidence_for(Path(args.snapshot))
            )

        outputs = args.output if args.output else None
        report = snap.restore(
            device,
            shot,
            outputs=outputs,
            dry_run=not args.apply,
            reason=args.reason,
        )

    print(report.summary())
    if not args.apply and not report.device_matches:
        print("\nDry run. Re-run with --apply to write these blocks.")
    return 0 if report.clean else 1


def cmd_idle(args) -> int:
    """Bring-up Stage 2: connect, send nothing, time the drop. Read-only."""
    session, transport = build_session(args)
    with session:
        identity = session.handshake()
        print(f"transport  {transport}")
        print(f"firmware   {identity.firmware}")
        print(
            f"\nGoing silent for {args.seconds:.0f} s. Sending nothing -- probing "
            f"during\nthe window would supply the very traffic being tested for."
        )
        result = session.measure_idle_survival(args.seconds)

    print(f"\n{result.summary()}")
    if result.unsolicited:
        print(
            f"\n!! {len(result.unsolicited)} UNSOLICITED frame(s). The device has "
            f"never done\n   this in 5834 captured frames. Record them:"
        )
        for frame in result.unsolicited:
            print(f"     {frame}")
    if result.survived:
        print(
            "\nThis is a LOWER BOUND. Re-run with a longer --seconds to bracket\n"
            "the timeout; one run can never show there isn't one."
        )
    return 0 if result.survived else 1


#: Stage 5's transition, and why it is this one rather than a flag.
#:
#: ``gain_raw`` 500 -> 490 on OUT1 is one dB, and it appears **literally** in
#: ``captures/btsnoop_hci.log`` -- the vendor app sent exactly this write while
#: the operator dragged the gain slider. So the first state-changing write this
#: project makes is a byte sequence the device has already accepted from
#: software it trusts. That is a materially stronger position than "a small
#: change we reasoned was safe", and it is only available for a value the
#: capture happens to contain.
STAGE5_BLOCK = 31
STAGE5_EXPECT_RAW = 500
STAGE5_TARGET_RAW = 490


def cmd_stage5(args) -> int:
    """Bring-up Stage 5: one real write, verified, then rolled back and verified.

    Deliberately **not** a general "set a parameter" command. This project has
    no business owning a tool that writes arbitrary bytes to arbitrary blocks
    before it has ever changed one on purpose, so the transition is hard-wired
    and the command refuses to run unless the device is in the exact state it
    expects. Same reasoning as ``rewrite_block_unchanged`` being a method
    rather than a flag: narrow the surface until the behaviour is proven.

    The rollback runs in the same invocation, in a ``finally``. A run that
    aborts halfway must leave the device no worse than it found it, and that
    is the operative half of the improvement invariant rather than a
    convenience.
    """
    session, transport = build_session(args)
    output = args.output - 1
    rc = 0
    with session:
        identity = session.handshake()
        device = _device(session, args)

        shot = snap.capture(
            device, identity, transport_name=transport, notes={"stage": "5-real"}
        )
        out = Path(args.snapshot_out)
        evidence = shot.save(out)

        before = device.block(output, STAGE5_BLOCK)
        live_raw = int.from_bytes(before[2:4], "little")

        print(f"transport      {transport}")
        print(f"restore point  {out}  ({evidence.digest[:16]})")
        print(f"target         OUT{args.output}, block {STAGE5_BLOCK} (OutputMisc)")
        print(f"gain_raw       {live_raw} -> {STAGE5_TARGET_RAW}")
        print(f"               {_dbfs(live_raw)} -> {_dbfs(STAGE5_TARGET_RAW)}")

        # Refuse on anything but the expected starting state. The capture's
        # precedent is for this exact transition; from a different starting
        # value it is a write nobody has ever seen this device accept.
        if live_raw != STAGE5_EXPECT_RAW:
            print(
                f"\nREFUSED. Expected gain_raw {STAGE5_EXPECT_RAW}, found "
                f"{live_raw}. This command only performs the transition the "
                f"capture contains; from any other value it would be an "
                f"unprecedented write. Nothing was transmitted."
            )
            return 2

        if not args.apply:
            print("\nDry run. Nothing was transmitted. Re-run with --apply.")
            return 0

        device.arm_writes(args.reason, evidence)
        try:
            print("\n-- the write ------------------------------------------")
            device.modify_block(
                output,
                STAGE5_BLOCK,
                lambda b: (
                    bytes(b[:2])
                    + STAGE5_TARGET_RAW.to_bytes(2, "little")
                    + bytes(b[4:])
                ),
                reason=args.reason,
            )
            after = device.block(output, STAGE5_BLOCK)
            got = int.from_bytes(after[2:4], "little")
            print(f"readback       gain_raw {got}  ({_dbfs(got)})")
            print(f"landed         {got == STAGE5_TARGET_RAW}")

            # The surrounding bytes are the point. The vendor app's gain writes
            # carry mute, polarity, delay, eq_mode and spk_type in the same
            # block, and a backend that reverted any of them would produce a
            # device the optimizer never modelled.
            untouched = after[:2] == before[:2] and after[4:] == before[4:]
            print(f"neighbours     {'preserved' if untouched else 'CHANGED'}")

            drift = snap.compare(device, shot)
            print(f"drift vs snap  {drift or 'only the block we wrote'}")
            if got != STAGE5_TARGET_RAW or not untouched:
                rc = 1
        finally:
            print("\n-- the rollback ---------------------------------------")
            report = snap.restore(
                device, shot, dry_run=False, reason="stage 5 rollback"
            )
            print(report.summary())
            residual = snap.compare(device, shot)
            print(f"device vs snapshot after rollback: {residual or 'identical'}")
            if residual or not report.clean:
                rc = 1

    print()
    if rc == 0:
        print("Stage 5 passed. A write changed the device, the readback proved")
        print("it, and the rollback put it back -- verified by re-reading, not")
        print("by assuming the restore worked.")
    else:
        print("Stage 5 FAILED. Read the rollback section above before touching")
        print("anything else; the snapshot on disk is the restore point.")
    return rc


def _dbfs(raw: int) -> str:
    return f"{raw / 10 - 60:+.1f} dB"


def cmd_stage6(args) -> int:
    """Bring-up Stage 6: many blocks, many channels, and the gang.

    Stage 5 wrote one block on one channel, twice. A fit writes tens of blocks
    across eight, so the things Stage 5 could not exercise are exactly the
    ones a tuning run depends on: the blast-radius caps, the backend's own
    multi-block ``write_channel``, and ``modify_block_mirrored`` -- the gang
    write, which has never run on silicon and guards two subwoofers in one
    ported box.

    Three parts, each restored and verified before the next begins, so a
    failure never compounds:

    A. **Multi-block, one channel** through :class:`Dsp408Spp.write_channel`
       -- the production path, not a bespoke sequence. One call writes misc,
       crossover, the fitted bands and (under EXCLUSIVE) flattens the rest.
    B. **Multi-channel**, the same across three outputs, so the blast radius
       is exercised above one.
    C. **The gang** on outputs 7/8: what the device does with an *unmirrored*
       write, then ``modify_block_mirrored``, then a readback proving both
       members hold one tune.
    """
    session, transport = build_session(args)
    rc = 0
    with session:
        identity = session.handshake()
        device = _device(session, args)
        backend = Dsp408Spp(device, peq_policy=PeqPolicy.EXCLUSIVE)

        shot = snap.capture(
            device, identity, transport_name=transport, notes={"stage": "6-multi"}
        )
        out = Path(args.snapshot_out)
        evidence = shot.save(out)

        partners = {ch: device.link_partners(ch) for ch in range(8)}
        ganged = sorted(ch for ch, p in partners.items() if p)

        print(f"transport      {transport}")
        print(f"restore point  {out}  ({evidence.digest[:16]})")
        print(f"link groups    {[f'OUT{c + 1}' for c in ganged] or 'none'}")
        for ch in TRIO:
            cfg = backend.read_channel(ch)
            print(
                f"OUT{ch + 1} now      {cfg.gain_dbfs:+.1f} dB, "
                f"{len(cfg.peq)} non-flat band(s), "
                f"hp {cfg.crossover.high_pass_hz:.0f} "
                f"lp {cfg.crossover.low_pass_hz:.0f}"
            )

        if not args.apply:
            print("\nDry run. Nothing was transmitted. Re-run with --apply.")
            return 0

        sendable, refused = _stage6_preflight(backend, device)
        device.arm_writes(args.reason, evidence)
        try:
            rc |= _stage6_multiblock(backend, device, shot, sendable)
            rc |= _stage6_multichannel(backend, device, shot, sendable)
            rc |= _stage6_gang(device, shot, partners)
            rc |= 0 if sendable else 1
        finally:
            print("\n-- final rollback -------------------------------------")
            report = snap.restore(
                device, shot, dry_run=False, reason="stage 6 rollback"
            )
            print(report.summary())
            residual = snap.compare(device, shot)
            print(f"device vs snapshot: {residual or 'identical'}")
            if residual or not report.clean:
                rc = 1

    print()
    print("Stage 6 passed." if rc == 0 else "Stage 6 FAILED -- read above.")
    return rc


#: The three outputs Stage 6 exercises. 1-3 rather than a random spread: they
#: are all on the ADAU at 0x37, so a multi-channel write stays inside one
#: chip's pool, and OUT1 is the only channel with any captured write precedent.
TRIO = (0, 1, 2)

SEP_PREFLIGHT = "\n-- 0. pre-flight (nothing transmitted) -----------------"


def _config_for(backend, ch, gain_delta_db: float):
    """The channel's live config with the gain nudged. Everything else as found.

    Built from ``read_channel`` rather than invented, so the crossover and
    bands are ones this device is already running -- the change under test is
    the *number of blocks written*, not the values.
    """
    live = backend.read_channel(ch)
    return ChannelConfig(
        gain_dbfs=live.gain_dbfs + gain_delta_db,
        delay_samples=live.delay_samples,
        crossover=Crossover(
            high_pass_hz=live.crossover.high_pass_hz,
            low_pass_hz=live.crossover.low_pass_hz,
            slope_db_oct=live.crossover.slope_db_oct,
        ),
        peq=live.peq,
        muted=live.muted,
    )


def _stage6_preflight(backend, device) -> tuple[list[int], dict[int, str]]:
    """Which of the trio can be written at all. Transmits nothing.

    Run **before** arming, because its answer decides what the rest of the
    stage does and because a refusal here is a finding rather than a fault.
    Real device state produced one on the first attempt: OUT1's plan flattens
    band 3 to ``freq`` 2514 / ``bw`` 42, whose frame checksum computes to zero
    at ``bluetooth_device_id`` 4, so the channel is unwritable under EXCLUSIVE
    until one of those numbers moves.
    """
    print(SEP_PREFLIGHT)
    sendable, refused = [], {}
    for ch in TRIO:
        plan = backend.plan_channel(ch, _config_for(backend, ch, -1.0))
        try:
            device.preflight(ch, [(b.data_id, b.payload) for b in plan])
        except UnsendablePlan as exc:
            refused[ch] = str(exc)
            print(f"OUT{ch + 1}  {len(plan):2d} block(s)  REFUSED")
            print(f"          {exc}")
            continue
        sendable.append(ch)
        print(f"OUT{ch + 1}  {len(plan):2d} block(s)  sendable")
    if not sendable:
        print("!! nothing is writable; parts A and B cannot run")
    return sendable, refused


def _stage6_multiblock(backend, device, shot, sendable) -> int:
    print("\n-- A. multi-block, one channel, via write_channel ------")
    if not sendable:
        print("skipped: no channel pre-flighted clean. NOT a pass.")
        return 1
    ch = sendable[0]
    print(f"channel         OUT{ch + 1}")
    before = device.stats.writes
    backend.write_channel(ch, _config_for(backend, ch, -1.0))
    written = device.stats.writes - before
    drift = snap.compare(device, shot)

    print(f"blocks written  {written}")
    print(f"channels moved  {sorted(drift)}")
    print(f"blocks moved    {drift.get(ch, [])}")

    ok = written >= 2 and sorted(drift) == [ch]
    if not ok:
        print("!! expected several blocks on one channel and nothing else")

    # `write_channel` under EXCLUSIVE relocates bands: read_channel compacts
    # non-flat bands to a leading run, so a band stored at index 3 is written
    # to index 0 and index 3 is flattened. Acoustically identical -- a biquad
    # cascade does not care about order -- and it is *why* a read-then-write
    # round trip must never be used as a no-op probe. Recorded, not asserted.
    print("note            relocation is expected under EXCLUSIVE; see docs")

    restored = snap.restore(device, shot, outputs=[ch], dry_run=False, reason="6A back")
    print(f"restored        {restored.clean}, {restored.total_writes} write(s)")
    return 0 if (ok and restored.clean) else 1


def _stage6_multichannel(backend, device, shot, sendable) -> int:
    print("\n-- B. multi-channel ------------------------------------")
    if len(sendable) < 2:
        print(f"skipped: only {len(sendable)} writable channel(s). NOT a pass.")
        return 1
    before = device.stats.writes
    for ch in sendable:
        backend.write_channel(ch, _config_for(backend, ch, -1.0))
    written = device.stats.writes - before
    drift = snap.compare(device, shot)
    print(f"blocks written  {written} across {len(sendable)} channels")
    print(f"channels moved  {sorted(drift)}")
    ok = sorted(drift) == sorted(sendable)
    if not ok:
        print(f"!! expected exactly {sorted(sendable)} to have moved")

    restored = snap.restore(
        device, shot, outputs=list(sendable), dry_run=False, reason="6B back"
    )
    print(f"restored        {restored.clean}, {restored.total_writes} write(s)")
    return 0 if (ok and restored.clean) else 1


def _stage6_gang(device, shot, partners) -> int:
    """The measurement only a bench with disconnected subwoofers can make."""
    print("\n-- C. the gang -----------------------------------------")
    members = [ch for ch, p in partners.items() if p]
    if not members:
        print("no link group on this device; nothing to test. NOT a pass.")
        return 1

    lead = members[0]
    peers = partners[lead]
    print(f"group           OUT{lead + 1} + {[f'OUT{p + 1}' for p in peers]}")
    print("read from       the device's linkgroup_num, not the app")

    # Does the device mirror? Measured from the capture on 2026-08-09 (the app
    # sends two writes ~10 ms apart), never from our own write. One unmirrored
    # write settles it directly. Safe here and nowhere else: outputs 7/8 drive
    # two subwoofers sharing a ported box, and leaving them unequal is the
    # mechanical-failure case -- which is why this runs only with nothing
    # connected, and is restored immediately.
    device.session.policy.acknowledge_gang({lead, *peers})
    peer_before = device.block(peers[0], 31)
    device.modify_block(
        lead,
        31,
        lambda b: bytes(b[:2]) + (360).to_bytes(2, "little") + bytes(b[4:]),
        reason="6C mirror probe",
    )
    peer_after = device.refresh(peers[0])[31 * 8 : 32 * 8]
    mirrored = peer_after != peer_before
    print(
        f"device mirrors  {mirrored}  <- expected False (app mirrors, device does not)"
    )

    snap.restore(device, shot, outputs=[lead, *peers], dry_run=False, reason="6C back")

    # Now the thing a tuning run actually does.
    before = device.stats.writes
    touched = device.modify_block_mirrored(
        lead,
        31,
        lambda b: bytes(b[:2]) + (360).to_bytes(2, "little") + bytes(b[4:]),
        reason="6C gang write",
    )
    print(
        f"mirrored write  touched {[f'OUT{c + 1}' for c in touched]}, "
        f"{device.stats.writes - before} block write(s)"
    )

    # Readback, not a comparison of what we sent. A partial write, a refused
    # frame or an off-by-one channel id all produce a mismatch here and none
    # is visible from the sending side.
    blocks = {ch: device.refresh(ch)[31 * 8 : 32 * 8] for ch in (lead, *peers)}
    gains = {ch: int.from_bytes(b[2:4], "little") for ch, b in blocks.items()}
    one_tune = len(set(gains.values())) == 1
    print(
        f"gang readback   {{{', '.join(f'OUT{c + 1}: {g}' for c, g in gains.items())}}}"
    )
    print(f"one tune        {one_tune}")

    restored = snap.restore(
        device, shot, outputs=[lead, *peers], dry_run=False, reason="6C back"
    )
    print(f"restored        {restored.clean}, {restored.total_writes} write(s)")
    return 0 if (one_tune and not mirrored and restored.clean) else 1


def cmd_noop_write(args) -> int:
    """Bring-up Stage 4: transmit a block's own bytes back. Dry run unless --apply.

    The first write in this project's history, shaped so it cannot change
    anything: the payload is the device's own current bytes, read live
    immediately beforehand.
    """
    session, transport = build_session(args)
    output = args.output - 1
    with session:
        identity = session.handshake()
        device = _device(session, args)

        # The restore point is captured *here*, in this invocation, rather than
        # named on the command line. A snapshot from an earlier session is
        # evidence about an earlier session.
        shot = snap.capture(
            device, identity, transport_name=transport, notes={"stage": "4-noop"}
        )
        out = Path(args.snapshot_out)
        evidence = shot.save(out)
        print(f"restore point  {out}  ({evidence.digest[:16]})")

        before = device.record(output)
        block = before[args.block * 8 : (args.block + 1) * 8]
        print(f"transport      {transport}")
        print(f"target         OUT{args.output}, block {args.block}")
        print(f"payload        {block.hex(' ')}  <- the device's own bytes")

        if not args.apply:
            print("\nDry run. Nothing was transmitted. Re-run with --apply.")
            return 0

        device.arm_writes(args.reason, evidence)
        sent = device.rewrite_block_unchanged(output, args.block, reason=args.reason)
        print(f"\nTRANSMITTED    {sent.hex(' ')}")

        after = device.refresh(output)
        drift = snap.compare(device, shot)

    if after != before:
        print("\n!! The record CHANGED. Restore from the snapshot and stop.")
        print("\n".join(describe_diff(before, after)))
        return 1
    if drift:
        print(f"\n!! Other channels moved: {sorted(drift)}. Restore and stop.")
        return 1

    print(f"\nOUT{args.output}'s 296-byte record is byte-identical, and so is every")
    print("other channel. The device acked a fragmented multi-chunk write.")
    print(
        "\nWhat this does NOT show: that the bytes were stored. They were\n"
        "already there, so a device that discarded the write looks the same.\n"
        "That is Stage 5."
    )
    return 0


#: How far ``fix-unsendable`` will move a band's frequency looking for a
#: checksum that is not zero. One hertz at 2.5 kHz is 0.7 cents; ten is seven,
#: still far below audibility. If nothing inside this window works the tool
#: refuses rather than widening it, because at some point "inaudible" stops
#: being true and nobody is watching.
MAX_FREQ_NUDGE_HZ = 10


def cmd_fix_unsendable(args) -> int:
    """Nudge the minimum needed to make a refused channel writable again.

    A frame whose checksum computes to zero cannot be sent, and the checksum is
    a function of the payload -- so a channel holding such a combination stays
    unwritable until a parameter moves. There is no fix on our side of the
    wire; this changes the *tune*, by the smallest amount that works.

    **This is the one command here that does not roll back.** Every other write
    in this tool restores what it changed; the whole point of this one is that
    the change persists. It still snapshots first.

    Scope is deliberately tight: EQ band frequency only, by at most
    ``MAX_FREQ_NUDGE_HZ``, and only on a band that is what makes the channel
    unwritable. It will not touch gain, crossover, bandwidth or level, and it
    refuses rather than searching further afield.
    """
    session, transport = build_session(args)
    rc = 0
    with session:
        identity = session.handshake()
        device = _device(session, args)
        backend = Dsp408Spp(device, peq_policy=PeqPolicy.EXCLUSIVE)

        shot = snap.capture(
            device,
            identity,
            transport_name=transport,
            notes={"stage": "fix-unsendable"},
        )
        out = Path(args.snapshot_out)
        evidence = shot.save(out)
        print(f"transport      {transport}")
        print(f"restore point  {out}  ({evidence.digest[:16]})")

        fixes = []
        for ch in range(N_OUTPUTS):
            blocked = _unsendable_blocks(backend, device, ch)
            if not blocked:
                continue
            print(f"\nOUT{ch + 1}  unwritable at block(s) {sorted(blocked)}")
            for data_id in sorted(blocked):
                fix = _find_nudge(device, ch, data_id)
                if fix is None:
                    print(f"  block {data_id}: NO FIX within {MAX_FREQ_NUDGE_HZ} Hz")
                    rc = 1
                    continue
                old, new = fix
                cents = 1200 * math.log2(new.freq / old.freq)
                print(
                    f"  block {data_id}: freq {old.freq} -> {new.freq} Hz "
                    f"({new.freq - old.freq:+d} Hz, {cents:+.2f} cents). "
                    f"level {old.level} and bw {old.bw} unchanged."
                )
                fixes.append((ch, data_id, new))

        if not fixes:
            print("\nNothing to fix." if rc == 0 else "\nUnfixable; see above.")
            return rc

        if not args.apply:
            print("\nDry run. Nothing was transmitted. Re-run with --apply.")
            print("NOTE: --apply here is PERMANENT. It is not rolled back.")
            return 0

        device.arm_writes(args.reason, evidence)
        for ch, data_id, band in fixes:
            device.modify_block(
                ch,
                data_id,
                lambda _b, x=band: x.encode(),
                reason="clear zero checksum",
            )
            print(f"\nwrote OUT{ch + 1} block {data_id}: {band.encode().hex(' ')}")

        # The point of the exercise: the channel must now plan and pre-flight.
        for ch in sorted({c for c, _, _ in fixes}):
            still = _unsendable_blocks(backend, device, ch)
            print(f"OUT{ch + 1} now writable: {not still}")
            if still:
                rc = 1

    print()
    print(
        "Fixed, and NOT rolled back -- that is the intent."
        if rc == 0
        else "FAILED; the snapshot above is the restore point."
    )
    return rc


def _unsendable_blocks(backend, device, ch) -> set[int]:
    """Blocks of ``ch`` that an EXCLUSIVE whole-channel write could not send."""
    live = backend.read_channel(ch)
    plan = backend.plan_channel(ch, live)
    bad = set()
    for block in plan:
        try:
            device.session.block_write_frame(ch, block.data_id, block.payload).encode()
        except UnsendableFrame:
            bad.add(block.data_id)
    return bad


def _find_nudge(device, ch, data_id):
    """Smallest frequency move making every frame this band appears in sendable.

    "Every frame" matters. The band has to be sendable where it is stored, in
    its flattened form (which is what a fit writes to a band it does not use),
    and at the leading index EXCLUSIVE relocates it to -- several payloads,
    several checksums, and fixing one can leave another at zero.
    """
    if not 0 <= data_id < ADDRESSABLE_BANDS:
        return None  # not an EQ band; this tool does not touch anything else
    stored = EqBand.decode(device.block(ch, data_id))

    def all_sendable(freq: int) -> bool:
        variants = [
            EqBand(
                freq=freq,
                level=stored.level,
                bw=stored.bw,
                shf_db=stored.shf_db,
                type=stored.type,
            ),
            EqBand(
                freq=freq,
                level=FLAT_LEVEL_RAW,
                bw=stored.bw,
                shf_db=stored.shf_db,
                type=stored.type,
            ),
        ]
        for index in {data_id, 0}:
            for band in variants:
                try:
                    device.session.block_write_frame(ch, index, band.encode()).encode()
                except UnsendableFrame:
                    return False
        return True

    for delta in range(1, MAX_FREQ_NUDGE_HZ + 1):
        for freq in (stored.freq + delta, stored.freq - delta):
            if freq > 0 and all_sendable(freq):
                return stored, EqBand(
                    freq=freq,
                    level=stored.level,
                    bw=stored.bw,
                    shf_db=stored.shf_db,
                    type=stored.type,
                )
    return None


def cmd_preset(args) -> int:
    """Bring-up: prove the preset store/recall rollback on hardware.

    **The last restore path never run on silicon**, and the one the closed
    loop's ARM stage depends on -- so it gets its own rung rather than being
    exercised for the first time buried inside a multi-stage tuning run, where
    a failure would be harder to attribute and would abort the run anyway.

    Known-answer, and the perturbation is the point: storing and immediately
    recalling proves nothing, because a recall that did nothing at all would
    pass. So this stores, **changes something**, recalls, and requires the
    change to be gone.

        snapshot -> store to the scratch slot -> confirm the working area
        did not move -> change gain on one output -> recall -> require the
        device to match the snapshot again

    Two distinct claims come out of it: that a store leaves the working area
    alone, and that a recall actually replaces it. The first is what makes a
    store safe to do at the start of a run; the second is what makes it a
    rollback.

    **This is destructive to the scratch slot**, permanently and with no undo.
    The slot must be named explicitly and its expendability attested -- the
    same shape as ``DriverCeiling`` and ``NoIsolation``, and for the same
    reason: it is a claim about the world that no code can check.
    """
    if not args.confirmed_by.strip():
        raise SystemExit("--confirmed-by must say who attests the slot is expendable")

    session, transport = build_session(args)
    rc = 0
    with session:
        identity = session.handshake()
        device = _device(session, args)

        if args.slot == identity.current_preset:
            print(
                f"REFUSED: slot {args.slot} is the preset the device is running "
                f"from. That slot is the operator's manual fallback -- recalling "
                f"it is how a person restores the car when nothing we wrote is "
                f"working. Pick another."
            )
            return 2

        shot = snap.capture(
            device, identity, transport_name=transport, notes={"stage": "preset"}
        )
        out = Path(args.snapshot_out)
        evidence = shot.save(out)

        print(f"transport      {transport}")
        print(f"restore point  {out}  ({evidence.digest[:16]})")
        print(f"scratch slot   {args.slot} ({identity.preset_names[args.slot - 1]!r})")
        print(f"attested by    {args.confirmed_by}")
        print(f"running from   preset {identity.current_preset}")
        print(f"\nStoring will DESTROY slot {args.slot}'s contents permanently.")

        if not args.apply:
            print("\nDry run. Nothing was transmitted. Re-run with --apply.")
            return 0

        device.arm_writes("preset bring-up", evidence)
        try:
            print("\n-- 1. store the working area to the slot ---------------")
            snap.store_as_preset(device, shot, args.slot, args.name)
            drift = snap.compare(device, shot)
            print(f"working area after the store: {drift or 'unchanged'}")
            if drift:
                print("!! a store must not touch the working area. Stopping.")
                return 1

            print("\n-- 2. change something, so the recall has work to do ---")
            before = int.from_bytes(device.block(0, 31)[2:4], "little")
            target = before - 10 if before >= 10 else before + 10
            device.modify_block(
                0,
                31,
                lambda b: bytes(b[:2]) + target.to_bytes(2, "little") + bytes(b[4:]),
                reason="preset bring-up: something to undo",
            )
            print(
                f"OUT1 gain_raw  {before} -> {target}  "
                f"({_dbfs(before)} -> {_dbfs(target)})"
            )
            print(f"device now differs at: {sorted(snap.compare(device, shot))}")

            print("\n-- 3. recall the slot ----------------------------------")
            report = snap.restore_from_preset(
                device, args.slot, expect=shot, dry_run=False
            )
            print(report.summary())

            after = int.from_bytes(device.block(0, 31)[2:4], "little")
            print(
                f"OUT1 gain_raw  {after}  "
                f"(expected {before}, the value before the change)"
            )

            residual = snap.compare(device, shot)
            print(f"device vs snapshot: {residual or 'identical'}")
            if residual or after != before:
                print("\n!! the recall did not restore the device. The block-by-")
                print("   block restore is still available; run `restore --apply`.")
                rc = 1
        finally:
            if rc:
                print("\n-- falling back to the block restore ------------------")
                fallback = snap.restore(
                    device, shot, dry_run=False, reason="preset bring-up fallback"
                )
                print(fallback.summary())
                print(
                    f"device vs snapshot: {snap.compare(device, shot) or 'identical'}"
                )

    print()
    if rc == 0:
        print("Preset rollback proven: a store leaves the working area alone,")
        print("and a recall really does replace it. That is the ~5 s restore")
        print("the improvement invariant wants, and it survives our process")
        print("dying, the host going away, and the snapshot file being lost.")
    else:
        print("FAILED. Read above; the snapshot on disk is still the restore point.")
    return rc


def cmd_reconcile(args) -> int:
    """After a crash: what did the journal intend, and what does the device hold?"""
    journal = WriteJournal.load(Path(args.journal))
    if not journal.entries:
        print(f"{args.journal} holds no entries.")
        return 0

    session, _ = build_session(args)
    with session:
        session.handshake()
        device = _device(session, args)
        results = device.reconcile(journal)

    for r in results:
        flag = "  " if r.outcome.value == "landed" else "**"
        print(
            f"{flag} OUT{r.entry.output + 1} block {r.entry.data_id:3d}  "
            f"{r.outcome.value:<12} device holds {r.actual}  "
            f"(intended {r.entry.after}, was {r.entry.before})"
        )
    unresolved = [r for r in results if r.needs_attention]
    print(f"\n{len(results)} entries, {len(unresolved)} needing attention")
    if unresolved:
        print(
            "  not_landed  = the block holds its pre-write value. Either the\n"
            "                write never arrived, or something put it back.\n"
            "  conflicting = neither value. Something else changed it."
        )
    return 1 if unresolved else 0


def cmd_rehearse(args) -> int:
    """Run the whole bring-up script in process, against the fake.

    Separate from the other subcommands because each of those is its own
    process, and ``--fake`` gets a brand-new device every time -- so a
    change-then-restore cycle cannot be demonstrated across two invocations.
    This runs the sequence end to end in one process, including the paths that
    are supposed to abort.

    Every stage here is one the operator will later run against hardware. If
    any of it fails, it fails now, on a device made of RAM.
    """
    import contextlib
    import tempfile

    from tuner.dsp.device import (
        ReadbackMismatch,
        UnverifiedBlock,
        WritesNotArmed,
    )

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp())
    workdir.mkdir(parents=True, exist_ok=True)
    steps: list[tuple[str, bool, str]] = []

    def step(name: str, ok: bool, detail: str = "") -> None:
        steps.append((name, ok, detail))
        tail = f" -- {detail}" if detail else ""
        print(f"  [{'ok' if ok else 'FAIL'}] {name}{tail}")

    def expect_raise(name: str, exc, fn) -> None:
        try:
            fn()
        except exc as e:
            step(name, True, f"{type(e).__name__}")
        except Exception as e:  # noqa: BLE001 - the point is to report anything
            step(name, False, f"raised {type(e).__name__} instead of {exc.__name__}")
        else:
            step(name, False, "did NOT raise")

    fake = FakeDsp408()
    session = Dsp408Session(
        LoopbackTransport(fake),
        policy=TxPolicy(
            allow_writes=True, blast_radius=BlastRadius(max_writes=99, max_channels=8)
        ),
        pacing=Pacing(idle_after_reply_s=0.0, max_requests_per_s=1e9),
    )
    session.open()
    journal = workdir / "journal.jsonl"
    device = Dsp408Device(
        session, journal=WriteJournal(journal), session_id="rehearsal"
    )

    print("Stage 1 -- link and enumerate (read-only)")
    identity = session.handshake()
    step(
        "connect ritual",
        session.stats.requests == 31,
        f"{session.stats.requests} requests",
    )
    step(
        "firmware recognised",
        identity.firmware.startswith("MYDW-AV"),
        identity.firmware,
    )
    step("eight channel records", len(identity.channels) == 8)

    print("\nStage 2 -- snapshot")
    shot = snap.capture(device, identity, transport_name="loopback(fake)")
    path = workdir / "rehearsal.json"
    evidence = shot.save(path)
    again = snap.capture(device, identity, transport_name="loopback(fake)")
    step("two snapshots agree", shot.digest == again.digest, shot.digest[:16])
    step("reloads from disk", snap.DeviceSnapshot.load(path).digest == shot.digest)

    # Part F's Stage 2 is the idle test, which is what this exercises. The
    # fake cannot answer the keepalive question -- a loopback has no idle
    # timeout to find -- so this rehearses the *mechanism* only: that we sit
    # silent, send nothing, and can still transact afterwards.
    requests_before = session.stats.requests
    idle = session.measure_idle_survival(0.3)
    step("idle window sent nothing", session.stats.requests == requests_before + 1)
    step("link survived and answered", idle.survived, idle.summary())
    step("no unsolicited frames", not idle.unsolicited)

    print("\nStage 3 -- refusals, before anything is armed")
    expect_raise(
        "unarmed write refused",
        WritesNotArmed,
        lambda: device.write_block(0, 31, bytes(8)),
    )
    expect_raise(
        "contradicted block 34 refused",
        UnverifiedBlock,
        lambda: device.write_block(0, 34, bytes(8)),
    )
    missing = workdir / "gone.json"
    expect_raise(
        "arming on a missing snapshot refused",
        WritesNotArmed,
        lambda: device.arm_writes(
            "x", snap.SnapshotEvidence(missing, "0" * 64, "MYDW-AV1.06", "rehearsal")
        ),
    )

    device.arm_writes("rehearsal", evidence)
    step("armed with a verified snapshot", device.armed)

    print("\nStage 4 -- the no-op write")
    # write_block deliberately declines to transmit a payload the device
    # already holds. Correct for a restore, and the reason Stage 4 needs its
    # own path rather than a flag: this assertion is about write_block, NOT
    # about Stage 4 having happened.
    same = device.block(0, 31)
    step(
        "write_block skips a payload already held",
        device.write_block(0, 31, same) is False,
    )

    before = device.record(0)
    writes_before = device.stats.writes
    sent = device.rewrite_block_unchanged(0, 31, reason="rehearsal stage 4")
    step("no-op write DID transmit", device.stats.writes == writes_before + 1)
    step("it sent the device's own bytes", sent == same, sent.hex(" "))
    step("record byte-identical afterwards", device.refresh(0) == before)
    step("no other channel moved", snap.compare(device, shot) == {})
    entry = device.journal.entries[-1]
    step(
        "journalled with before == after",
        entry.before == entry.after == same.hex(),
        "the durable signature of a no-op probe",
    )
    expect_raise(
        "no-op refused on a contradicted block",
        UnverifiedBlock,
        lambda: device.rewrite_block_unchanged(0, 34),
    )

    print("\nStage 5 -- one real write, then roll it back")
    device.modify_block(
        0,
        31,
        lambda b: bytes(b[:2]) + (490).to_bytes(2, "little") + bytes(b[4:]),
        reason="gain 500->490",
    )
    drift = snap.compare(device, shot)
    step("change detected", drift == {0: [31]}, str(drift))
    dry = snap.restore(device, shot, dry_run=True)
    step("dry run writes nothing", dry.total_writes == 0 and dry.blocks_to_write == 1)
    applied = snap.restore(device, shot, dry_run=False)
    step("rollback verified", applied.clean and applied.total_writes == 1)
    step("device matches the snapshot", snap.compare(device, shot) == {})

    print("\nStage 6 -- multi-channel, and the blocked case")
    for ch in range(3):
        device.modify_block(
            ch,
            31,
            lambda b: bytes(b[:2]) + (480).to_bytes(2, "little") + bytes(b[4:]),
            reason="multi",
        )
    report = snap.restore(device, shot, dry_run=False)
    step("three channels restored", report.clean and report.total_writes == 3)

    fake.image.channels[5][34 * 8 : 35 * 8] = bytes([0xFF] * 8)
    expect_raise(
        "contradicted-block drift stops the restore",
        snap.RestoreBlocked,
        lambda: snap.restore(device, shot, dry_run=False),
    )
    fake.image.channels[5][34 * 8 : 35 * 8] = shot.block(5, 34)

    print("\nStage 7 -- crash mid-write, then reconcile")
    original_write = fake._write

    def die(frame):
        raise KeyboardInterrupt("simulated crash after journalling")

    fake._write = die
    with contextlib.suppress(KeyboardInterrupt):
        device.modify_block(1, 32, lambda b: bytes([9]) + bytes(b[1:]), reason="crash")
    fake._write = original_write

    entries = WriteJournal.load(journal).entries
    step("the crashed write was journalled", entries and entries[-1].data_id == 32)
    results = device.reconcile(WriteJournal.load(journal))
    unresolved = [r for r in results if r.needs_attention]
    step(
        "reconcile classifies it as not landed",
        any(
            r.entry.data_id == 32 and r.outcome.value == "not_landed"
            for r in unresolved
        ),
    )
    step("device still matches the snapshot", snap.compare(device, shot) == {})

    print("\nStage 8 -- a device that lies about a write")

    def swallow(frame):
        return None

    fake._write = swallow
    expect_raise(
        "readback catches a swallowed write",
        ReadbackMismatch,
        lambda: device.modify_block(
            2, 31, lambda b: bytes(b[:2]) + (400).to_bytes(2, "little") + bytes(b[4:])
        ),
    )
    fake._write = original_write

    session.close()

    failed = [s for s in steps if not s[1]]
    print(f"\n{len(steps) - len(failed)}/{len(steps)} steps passed")
    print(f"workdir: {workdir}")
    if failed:
        print("\nFAILED:")
        for name, _, detail in failed:
            print(f"  {name} -- {detail}")
        return 1
    print("\nEvery stage of the bring-up script ran, including all abort paths.")
    return 0


# -- wiring -----------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--fake", action="store_true", help="run against the in-process fake"
    )
    ap.add_argument("--address", help="Bluetooth address of a paired DSP-408")
    ap.add_argument("--channel", type=int, default=1, help="RFCOMM server channel")
    ap.add_argument("--port", help="outgoing Bluetooth SPP COM port, e.g. COM7")
    ap.add_argument(
        "--link-id",
        type=int,
        default=OBSERVED_BLUETOOTH_DEVICE_ID,
        metavar="N",
        help=(
            "bluetooth_device_id to stamp on every frame. Default "
            f"{OBSERVED_BLUETOOTH_DEVICE_ID}, the value in all 5834 frames of "
            "the phone capture. It is almost certainly a host-side "
            "paired-device index, so a PC pairing may well differ -- if it "
            "does, the session raises LinkIdMismatch naming the value the "
            "device used, and you pass that back here. Read-only stages are "
            "the place to find out."
        ),
    )
    ap.add_argument(
        "--fake-from",
        metavar="SNAPSHOT",
        help=(
            "seed --fake with a real device's records, so a rehearsal sees the "
            "real link groups and band layout instead of a uniform flat image"
        ),
    )
    ap.add_argument("--journal", help="path to the write journal")
    ap.add_argument("--session-id", help="label recorded in snapshots and the journal")

    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("enumerate", help="connect and report what the device says")
    p.set_defaults(func=cmd_enumerate)

    p = sub.add_parser("snapshot", help="capture a restore point")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--note", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument(
        "--ddp-template",
        help="a .DDP from THIS device; also writes a vendor-loadable backup",
    )
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("verify", help="compare the live device against a snapshot")
    p.add_argument("snapshot")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("diff", help="diff two snapshot files")
    p.add_argument("before")
    p.add_argument("after")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("restore", help="put the device back to a snapshot")
    p.add_argument("snapshot")
    p.add_argument(
        "--apply",
        action="store_true",
        help="actually write. Without this it is a dry run.",
    )
    p.add_argument(
        "--output",
        type=int,
        action="append",
        help="restore only this output (0-based). Repeatable.",
    )
    p.add_argument("--reason", default="rollback")
    p.add_argument("--max-writes", type=int, default=64)
    p.add_argument("--max-channels", type=int, default=8)
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser(
        "idle",
        help="Stage 2: connect, send nothing, time the drop (read-only)",
    )
    p.add_argument(
        "--seconds",
        type=float,
        default=120.0,
        help=(
            "how long to stay silent. One run gives a LOWER BOUND, never the "
            "timeout -- ladder it (30, 120, 300) across separate runs."
        ),
    )
    p.set_defaults(func=cmd_idle)

    p = sub.add_parser(
        "noop-write",
        help="Stage 4: send a block's own bytes back. The first write.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="actually transmit. Without this it is a dry run.",
    )
    p.add_argument(
        "--snapshot-out",
        required=True,
        help=(
            "where to write the restore point. Captured in THIS invocation on "
            "purpose: a snapshot from an earlier session is evidence about an "
            "earlier session."
        ),
    )
    p.add_argument(
        "--output",
        type=int,
        default=1,
        help=(
            "1-based output. Default 1, because every one of the capture's 21 "
            "writes went to channel_id 0 -- writes to 1-7 have no precedent."
        ),
    )
    p.add_argument(
        "--block",
        type=int,
        default=31,
        help=(
            "block index (offset = block*8). Default 31, OutputMisc: the block "
            "the captured gain writes targeted, and one whose every byte is "
            "decoded."
        ),
    )
    p.add_argument("--reason", default="bring-up stage 4: no-op write")
    p.add_argument("--max-writes", type=int, default=1)
    p.add_argument("--max-channels", type=int, default=1)
    p.set_defaults(func=cmd_noop_write)

    p = sub.add_parser(
        "stage5",
        help="Stage 5: one real write (gain 500->490 on OUT1), then roll it back",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="actually transmit. Without this it is a dry run.",
    )
    p.add_argument(
        "--snapshot-out",
        required=True,
        help="where to write the restore point, captured in THIS invocation",
    )
    p.add_argument(
        "--output",
        type=int,
        default=1,
        help="1-based output. Default 1; the capture's writes all went there.",
    )
    p.add_argument("--reason", default="bring-up stage 5: gain 500->490")
    # Two: the write out and the write back. Anything more means the restore
    # found drift nobody predicted, and it should hit the cap rather than
    # quietly repair a device we have stopped understanding.
    p.add_argument("--max-writes", type=int, default=2)
    p.add_argument("--max-channels", type=int, default=1)
    p.set_defaults(func=cmd_stage5)

    p = sub.add_parser(
        "stage6",
        help="Stage 6: multi-block, multi-channel, and the gang",
    )
    p.add_argument("--apply", action="store_true", help="actually transmit")
    p.add_argument("--snapshot-out", required=True)
    p.add_argument("--reason", default="bring-up stage 6: multi-block/channel/gang")
    # Wider than every earlier stage, deliberately and only here. Part B writes
    # three channels and part C writes a gang, so a cap of 1 would refuse the
    # thing under test. The number is an upper bound on a run that restores
    # after each part, not a licence for a loop to walk the device.
    p.add_argument("--max-writes", type=int, default=200)
    p.add_argument("--max-channels", type=int, default=8)
    p.set_defaults(func=cmd_stage6)

    p = sub.add_parser(
        "fix-unsendable",
        help="nudge a band's frequency so a refused channel is writable. PERMANENT.",
    )
    p.add_argument("--apply", action="store_true", help="actually transmit")
    p.add_argument("--snapshot-out", required=True)
    p.add_argument("--reason", default="clear a zero-checksum frame")
    p.add_argument("--max-writes", type=int, default=8)
    p.add_argument("--max-channels", type=int, default=8)
    p.set_defaults(func=cmd_fix_unsendable)

    p = sub.add_parser(
        "preset",
        help="prove the preset store/recall rollback. DESTROYS the scratch slot.",
    )
    p.add_argument("--apply", action="store_true", help="actually transmit")
    p.add_argument("--snapshot-out", required=True)
    p.add_argument("--slot", type=int, required=True, help="scratch preset slot, 1-6")
    p.add_argument(
        "--confirmed-by",
        required=True,
        help="who attests this slot is expendable, recorded verbatim",
    )
    p.add_argument("--name", default="tuner-baseline")
    p.add_argument("--reason", default="preset bring-up")
    p.add_argument("--max-writes", type=int, default=64)
    p.add_argument("--max-channels", type=int, default=8)
    p.set_defaults(func=cmd_preset)

    p = sub.add_parser("reconcile", help="journal vs device, after a crash")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser(
        "rehearse",
        help="run the whole bring-up script in process against the fake",
    )
    p.add_argument("--workdir", help="where to leave the snapshot and journal")
    p.set_defaults(func=cmd_rehearse, fake=True)

    args = ap.parse_args()
    if args.command == "reconcile" and not args.journal:
        ap.error("reconcile needs --journal")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
