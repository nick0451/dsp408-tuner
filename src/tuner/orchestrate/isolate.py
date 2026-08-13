"""Making exactly one driver audible, and proving it.

The operator's method, which this automates: tune output 1 by muting 2-8 in
the vendor app, confirm visually that they show as muted, sweep. That is the
right shape and it works. Two things about it change when the loop is closed.

**A visual check of the app is weaker than a readback of the device.** They
are not the same claim: the app shows what it believes, and this project has
already caught it believing wrongly -- in two captures it displayed a pair as
linked while ``linkgroup_num`` was stored as 0. A readback of the enabled byte
costs one transaction and says what the *device* holds. So the app's display
is not the check here; the device's answer is. That is not a criticism of the
manual method -- with a human in the loop, a visual check plus ears is a fine
belt and braces. With no human, the readback is what remains.

**Neither one is a measurement.** Hard safety rule 5 says never assume a
channel is silent because you did not address it, and verify routing by
measurement rather than intent. A readback proves the byte we wrote is the
byte stored; it does not prove that byte silences the amplifier, that the
microphone is not hearing a path that bypasses the DSP entirely, or that the
output we think we unmuted is the one wired to the driver in front of the
microphone.

:meth:`MuteIsolator.prove_silence` closes that, once per session, using
machinery the project already has: mute **everything**, sweep, and require the
capture to come back silent. ``capture_sweep`` raises :class:`SilentPath` when
the stimulus does not arrive, so the proof is that existing check inverted --
a sweep that *succeeds* here is the failure, and it means something is making
sound that mute does not control.

Endurance
---------
Every mute write is immediately non-volatile. Isolation is therefore not free:
a naive implementation rewrites eight channels for every channel switch. The
underlying read-modify-write skips a write that would change nothing, so
switching isolation from output *n* to output *n+1* costs **two** writes -- one
unmute, one mute -- not sixteen. Endurance figures for the part are not known
and must not be invented, but two-per-switch versus sixteen is worth having
without needing a number.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ..dsp.dsp408_spp import Dsp408Spp
from ..dsp.session import N_OUTPUTS
from ..dsp.snapshot import DeviceSnapshot
from ..measure.qa import SilentPath


class IsolationError(RuntimeError):
    """The run cannot establish, or cannot prove, single-driver isolation."""


class Isolator(Protocol):
    """Makes one output audible and the rest silent, between sweeps."""

    def begin(self, snapshot: DeviceSnapshot) -> None:
        """Record the state to return to. Called once, after the baseline snapshot."""
        ...

    def prove_silence(self, sweep: Callable[[], object]) -> None:
        """Establish, by measurement, that isolation actually silences.

        Called once per run, before any sweep that will be scored.
        """
        ...

    def isolate(self, outputs: Sequence[int]) -> None:
        """Make ``outputs`` the only audible ones. Verified, not just written.

        Takes a set rather than a single index because a gang -- two
        subwoofers in one box, say -- is one thing to measure and cannot be
        separated even in principle.
        """
        ...

    def restore(self) -> None:
        """Put every output back to the state ``begin`` recorded."""
        ...

    def describe(self) -> dict[str, object]:
        """Provenance: how isolation was achieved, and what proved it."""
        ...


@dataclass
class NoIsolation:
    """Nothing is muted, because nothing else can make sound.

    The bench case: one output wired to one input, everything else physically
    disconnected. Legitimate, and much the safest configuration to run in --
    but it is a claim about the world that no code can check, so it requires
    a written ``basis`` naming what is connected and what is not. The basis
    goes into the run's provenance.

    Same shape as :class:`~tuner.orchestrate.plan.DriverCeiling`: the way to
    make a safety assumption deliberate is to make somebody type the reason.
    """

    basis: str

    def __post_init__(self) -> None:
        if not self.basis or not self.basis.strip():
            raise IsolationError(
                "NoIsolation asserts that no other output can make sound, "
                "which nothing here can verify. Say what is connected and "
                "what is not; it is recorded in the run's provenance."
            )

    def begin(self, snapshot: DeviceSnapshot) -> None:
        del snapshot

    def prove_silence(self, sweep: Callable[[], object]) -> None:
        """Deliberately does not sweep.

        There is nothing to prove -- the claim is that no other output is
        connected, which a sweep cannot confirm either way -- and emitting a
        stimulus to learn nothing is a stimulus that should not be emitted.
        The claim is carried in :attr:`basis` and lands in provenance instead.
        """
        del sweep

    def isolate(self, outputs: Sequence[int]) -> None:
        del outputs

    def restore(self) -> None:
        pass

    def describe(self) -> dict[str, object]:
        return {"method": "none", "basis": self.basis, "proved_silent": False}


@dataclass
class MuteIsolator:
    """Mutes every output but one, the way the operator does it by hand.

    Attributes:
        backend: The DSP. Mute goes through
            :meth:`~tuner.dsp.dsp408_spp.Dsp408Spp.set_muted`, which touches
            one block and skips no-op writes.
        outputs: Which outputs to manage. Defaults to all eight -- an output
            left out of this set is one the isolator will never mute, which is
            exactly the assumption rule 5 forbids, so narrowing it needs a
            reason as good as ``NoIsolation``'s.
        gangs: Declared groups that move as one, from the plan. Members are
            muted and unmuted together, and a device link group must be
            covered by exactly one of them.
    """

    backend: Dsp408Spp
    outputs: tuple[int, ...] = tuple(range(N_OUTPUTS))
    gangs: tuple[tuple[int, ...], ...] = ()
    _baseline: DeviceSnapshot | None = field(default=None, init=False)
    _proved_silent: bool = field(default=False, init=False)
    _current: tuple[int, ...] = field(default=(), init=False)
    _writes: int = field(default=0, init=False)
    _warnings: list[str] = field(default_factory=list, init=False)

    # -- lifecycle ---------------------------------------------------------

    def begin(self, snapshot: DeviceSnapshot) -> None:
        """Record where to return to, and reconcile gangs against the device.

        Two directions of disagreement, and they get different treatment.

        **The device says linked, no gang covers it** -- a hard stop. The pair
        may be sharing an enclosure, and separating it could destroy a driver.
        This isolator cannot know which, so it refuses and asks for a decision.

        **A gang is declared, the device says unlinked** -- a warning, not a
        stop. That is the observed state of outputs 7 and 8 in 14 of the 40
        ``.DDP`` backups, and the run is *safer* than the device here: it
        writes every member identically because the gang says so, whatever the
        flag reads. What the warning records is that the device is in a state
        where the vendor app could later move one member alone.
        """
        self._warnings = []
        linked = snapshot.linked_channels()
        ganged = {o for gang in self.gangs for o in gang}

        for gang in self.gangs:
            if len(gang) > 1 and not set(gang) & linked:
                self._warnings.append(
                    f"gang {list(gang)} is declared but the device stores "
                    f"linkgroup_num as 0 for every member. The run will still "
                    f"write them identically -- the declaration governs, not "
                    f"the flag -- but the vendor app can move one of them "
                    f"alone in this state. Re-link them in the app if they "
                    f"share an enclosure."
                )

        for gang in self.gangs:
            partners = {p for o in gang if o in linked for p in _group_of(snapshot, o)}
            stray = sorted(partners - set(gang))
            if stray:
                raise IsolationError(
                    f"gang {list(gang)} covers part of a device link group; "
                    f"outputs {stray} are linked to it but are not members. "
                    f"A half-covered group would be measured together and "
                    f"written apart. Either add them to the gang or unlink."
                )

        conflicts = sorted((set(self.outputs) & linked) - ganged)
        if conflicts:
            raise IsolationError(
                f"outputs {conflicts} are in a link group, and this isolator "
                f"cannot separate a linked pair: muting one half either mutes "
                f"both or leaves the device disagreeing with the model. "
                f"**Do not resolve this by unlinking.** A link can exist "
                f"because two drivers share an enclosure, in which case the "
                f"pair is one acoustic source and mismatching them is a "
                f"mechanical hazard rather than a tuning choice -- on this "
                f"system outputs 7 and 8 are two subwoofers in one ported box. "
                f"Such a pair has to be measured together and corrected "
                f"identically, which needs gang support this isolator does not "
                f"yet have; exclude them from the run until it does. A pair "
                f"linked only for convenience may be unlinked, but do it "
                f"**before** the baseline snapshot: unlinking is a device "
                f"state change and the run would otherwise roll back into it."
            )
        self._baseline = snapshot
        self._current = ()

    def isolate(self, outputs: Sequence[int]) -> None:
        wanted = tuple(sorted(set(int(o) for o in outputs)))
        if self._baseline is None:
            raise IsolationError("begin() has not been called; nothing to return to")
        if not wanted:
            raise IsolationError("isolate() needs at least one output")
        stray = sorted(set(wanted) - set(self.outputs))
        if stray:
            raise IsolationError(
                f"outputs {stray} are not ones this isolator manages "
                f"({list(self.outputs)})"
            )
        if not self._proved_silent:
            raise IsolationError(
                "isolation has not been proved by measurement. Call "
                "prove_silence() once per session: mute everything, sweep, and "
                "require silence. A readback proves the byte we wrote is the "
                "byte stored; it does not prove that byte silences the driver, "
                "or that the microphone is not hearing a path that bypasses "
                "the DSP. Rule 5: verify routing by measurement, not intent."
            )
        self._set_all(audible=wanted)
        self._verify(audible=wanted)
        self._current = wanted

    def restore(self) -> None:
        """Back to the mute states the baseline snapshot recorded."""
        if self._baseline is None:
            return
        for channel in self.outputs:
            self._writes += len(
                self.backend.set_muted(channel, self._baseline_muted(channel))
            )
        self._current = ()

    # -- the proof ---------------------------------------------------------

    def prove_silence(self, sweep: Callable[[], object]) -> None:
        """Mute everything, sweep, and require the path to be silent.

        ``sweep`` is a zero-argument callable that runs one capture and either
        returns or raises :class:`~tuner.measure.qa.SilentPath`. In the run
        this is the ordinary measurer with an arbitrary output; the output
        does not matter, because every one of them is muted.

        A sweep that **succeeds** is the failure. Something is making sound
        that mute does not control: a path around the DSP, an output we are
        not managing, or ``enabled`` not meaning on this unit what it meant on
        the one the A/B was taken from.
        """
        if self._baseline is None:
            raise IsolationError("begin() has not been called")
        self._set_all(audible=())
        self._verify(audible=())
        try:
            sweep()
        except SilentPath:
            self._proved_silent = True
            return
        raise IsolationError(
            "every output is muted and the microphone still hears the "
            "stimulus. Isolation by mute does not work on this rig, so no "
            "single-driver measurement from it means what it appears to. "
            "Likely causes, in order: the microphone is picking up a path "
            "that does not go through the DSP; the driver in front of it is "
            "fed by an output this isolator is not managing; or the mute "
            "control does not silence this output. Do not tune until this is "
            "understood."
        )

    # -- internals ---------------------------------------------------------

    def _baseline_muted(self, channel: int) -> bool:
        assert self._baseline is not None
        # Byte 0 of the MISC block: 1 = on, 0 = muted. Read from the snapshot
        # rather than from the live device, because the live device is
        # whatever the last isolate() left it as.
        from ..dsp.protocol import OutputBlock, OutputMisc

        block = self._baseline.block(channel, int(OutputBlock.MISC))
        return OutputMisc.decode(block).enabled == 0

    def _set_all(self, audible: Sequence[int]) -> None:
        audible = set(audible)
        for channel in self.outputs:
            self._writes += len(
                self.backend.set_muted(channel, muted=channel not in audible)
            )

    def _verify(self, audible: Sequence[int]) -> None:
        """Read the mute state back from the device. The app's display is not this."""
        audible = set(audible)
        wrong = []
        for channel in self.outputs:
            want_muted = channel not in audible
            if self.backend.read_channel(channel).muted != want_muted:
                wrong.append(channel)
        if wrong:
            raise IsolationError(
                f"outputs {wrong} did not read back at the requested mute "
                f"state after being written. The device does not hold what we "
                f"asked for, so a sweep now would measure an unknown set of "
                f"drivers."
            )

    @property
    def warnings(self) -> tuple[str, ...]:
        """Disagreements between the declared gangs and the device's own flag."""
        return tuple(self._warnings)

    def describe(self) -> dict[str, object]:
        return {
            "method": "dsp-mute",
            "managed_outputs": list(self.outputs),
            "gangs": [list(g) for g in self.gangs],
            "proved_silent": self._proved_silent,
            "mute_writes": self._writes,
            "warnings": list(self._warnings),
        }


def _group_of(snapshot: DeviceSnapshot, output: int) -> frozenset[int]:
    """Every output sharing ``output``'s non-zero link group, itself included."""
    from ..dsp.protocol import OutputBlock

    def group(channel: int) -> int:
        return snapshot.block(channel, int(OutputBlock.DYNAMICS))[7]

    mine = group(output)
    if not mine:
        return frozenset()
    return frozenset(ch for ch in range(N_OUTPUTS) if group(ch) == mine)


def outputs_from(sequence: Sequence[int]) -> tuple[int, ...]:
    """Validate and normalise a managed-output set."""
    outputs = tuple(sorted(set(int(o) for o in sequence)))
    for output in outputs:
        if not 0 <= output < N_OUTPUTS:
            raise IsolationError(f"output {output} outside 0..{N_OUTPUTS - 1}")
    return outputs
