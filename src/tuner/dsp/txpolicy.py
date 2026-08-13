"""What this project is permitted to put on the wire.

The device has **no undo**, every write is immediately non-volatile, and the
operator has exactly one unit. So the question is not "is this frame
well-formed" -- :mod:`tuner.dsp.protocol` answers that -- but "has the vendor
app ever been observed doing this". Anything else is an experiment on
irreplaceable hardware.

Pure logic, no I/O. Every rule here is checked against
``captures/btsnoop_hci.log`` in ``tests/test_txpolicy.py``: all 2918 host frames
must pass, and the things we refuse must fail.

Why an allow-list, when a blacklist already exists
--------------------------------------------------
``protocol.DESTRUCTIVE_COMMANDS`` blocks ``RESET_MCU``, ``TRANSMITTAL`` and
``RESET_GROUP_DATA``. It is a good rule and it stays. But the capture shows it
is **inert**: ``DataType 9`` with ``ChannelID`` 95-99 appears **zero times** in
16 928 packets, and a raw byte scan for those opcodes finds nothing either. It
has never prevented anything, and it never would in a normal session.

That is the general failure of blacklists here. We know three dangerous opcodes
because we found them by decompiling; the space of ``(data_type, channel_id,
data_id)`` is 2**24 and we have observed 32 tuples of it. **The unknown-dangerous
set is everything we have not looked at**, and enumerating it is impossible.

Inverting the question makes it answerable: permit what the vendor app was seen
to do, refuse everything else, and require each exception to be argued. The
blacklist stays underneath as defence in depth -- if a bug ever constructs a
`RESET_MCU`, two independent rules have to fail for it to reach the device.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .protocol import (
    BLOCK_PAYLOAD_LEN,
    OUTPUT_BULK_LEN,
    PRESET_NAME_WRITE_LEN,
    PRESET_SLOT_MAX,
    PRESET_SLOT_WORKING_AREA,
    DataType,
    Frame,
    FrameType,
    OutputBlock,
)

#: Output ``data_id`` values that may be read. 0-30 are EQ bands, 31-36 the
#: named blocks, 119 the whole-channel bulk read.
READABLE_OUTPUT_IDS = frozenset(range(0, 37)) | {119}

#: Output ``data_id`` values that may be written.
#:
#: Excludes 34 and 35 -- their meaning is contradicted between the decompiled
#: app and the device's own readback (see :class:`~tuner.dsp.protocol.OutputBlock`).
#: Excludes 119: a bulk write at *that* DataID has still never been observed.
#:
#: Corrected 2026-08-09: a 296-byte bulk write **does** exist, but it is
#: ``data_id`` 0 with ``user_id`` set to a preset slot, and it writes the slot
#: rather than the working area. ``data_id`` 0 is therefore overloaded -- an
#: 8-byte payload is EQ band 0, a 296-byte payload is a whole channel record --
#: and the only thing separating them on the wire is the length. The write path
#: already requires exactly ``BLOCK_PAYLOAD_LEN``, which refuses the bulk form;
#: that is now load-bearing rather than incidental, so do not relax it.
WRITABLE_OUTPUT_IDS = frozenset(range(0, 34)) | {int(OutputBlock.NAME)}

#: ``DataType 9`` channels the app reads during its connect ritual. Named for
#: what they are where that is known, and left blank where it is not -- do not
#: invent meanings.
SYSTEM_READ_CHANNELS: dict[int, str] = {
    0: "preset name (user_id selects the slot)",
    2: "unknown",
    3: "status/metering poll",
    4: "firmware string",
    5: "unknown",
    6: "unknown",
    7: "unknown, empty reply",
    8: "unknown, empty reply",
    19: "unknown",
    52: "current preset slot",
}


class TxRefused(RuntimeError):
    """Raised when a frame is not on the allow-list.

    Deliberately not a subclass of :class:`~tuner.dsp.protocol.ProtocolError`:
    the frame is usually perfectly well-formed. What is wrong is that we have
    no evidence the device expects it.
    """


@dataclass(frozen=True)
class BlastRadius:
    """Caps on how much damage one session can do before something stops it.

    These do not prevent a wrong write; readback verification and the snapshot
    do that. What they bound is **how many wrong writes happen before anyone
    notices** -- the failure where a loop with an off-by-one walks all eight
    channels in three seconds.

    ``max_channels`` defaults to 1 because the bring-up sequence writes one
    channel at a time and verifies each by whole-record readback before moving
    on. Raising it is a deliberate act.
    """

    max_writes: int = 64
    max_channels: int = 1

    def __post_init__(self) -> None:
        if self.max_writes < 0 or self.max_channels < 0:
            raise ValueError("caps must not be negative")


@dataclass
class TxPolicy:
    """Decides whether a frame may be transmitted, and counts what was.

    Stateful: it tracks the writes and channels used so far in the session so
    :class:`BlastRadius` can be enforced. One policy per session.
    """

    blast_radius: BlastRadius = field(default_factory=BlastRadius)
    allow_writes: bool = False
    refuse_linked_channels: bool = True

    #: Permit preset store and recall. Separate from :attr:`allow_writes`
    #: because they are a different kind of risk, in both directions: a recall
    #: replaces all eight channels at once, which is catastrophic by accident
    #: and is *exactly what rollback needs* on purpose. A store touches no
    #: audio at all. Neither belongs behind the same switch as "may write one
    #: parameter block".
    allow_presets: bool = False

    writes: int = field(default=0, init=False)
    preset_writes: int = field(default=0, init=False)
    channels_written: set[int] = field(default_factory=set, init=False)
    _linked: frozenset[int] = field(default_factory=frozenset, init=False)
    _acknowledged: frozenset[int] = field(default_factory=frozenset, init=False)

    def set_linked_channels(self, channels: frozenset[int] | set[int]) -> None:
        """Record which outputs are in a link group, from a device snapshot.

        **Measured 2026-08-09: the app mirrors, the device does not.**

        Three gain steps on output 7 while the pair was linked produced *six*
        writes -- ``ch=6`` and ``ch=7`` about 10 ms apart, carrying identical
        gains. The same three steps after unlinking produced *three*, on one
        channel only::

            linked    22:38:32 ch=6 id=31  01 00 a4 01 00 00 00 0f   gain 420
                      22:38:32 ch=7 id=31  01 00 a4 01 00 00 00 12   gain 420
            unlinked  22:39:35 ch=6 id=31  01 00 86 01 00 00 00 0f   gain 390
                                (no ch=7)

        Byte 7 differs between the two -- 15 and 18, each channel's own
        ``spk_type`` -- so these are ordinary read-modify-writes, not a
        broadcast.

        **So a single write is safe and predictable: it changes exactly the
        channel addressed.** What the link costs us is not a device hazard but a
        modelling one -- write one half of a linked pair and the device no
        longer matches what the optimizer reasoned about, and the improvement
        invariant then compares a prediction against a system configured
        differently.

        The refusal therefore stays **on by default**, but its meaning has
        changed: it is no longer "we do not know what this does", it is "decide
        whether you meant to mirror". Turn it off and mirror the write yourself
        -- :meth:`~tuner.dsp.device.Dsp408Device.link_partners` exists for
        exactly that.

        One thing to know before trusting a device's link state: in two separate
        captures the vendor app **wrote unlinking but never wrote re-linking**.
        The app can show a pair linked while the device has ``linkgroup_num``
        stored as 0. Read the link group from the device, never from what the
        app displayed.
        """
        self._linked = frozenset(channels)

    def acknowledge_gang(self, outputs: frozenset[int] | set[int]) -> None:
        """Permit writes to linked outputs the caller writes as a single unit.

        The linked-channel refusal exists because writing one half of a pair
        leaves the device disagreeing with the model. A caller that writes
        *every* member with the same values has not created that disagreement,
        so the refusal is the wrong answer for it -- but only that caller can
        know it, so it has to say so.

        **This policy cannot check the claim.** It holds a flat set of linked
        channels, not the groups, so it cannot tell a fully-covered group from
        a half-covered one. The caller must verify completeness against
        :meth:`~tuner.dsp.device.Dsp408Device.link_partners` before calling
        this; :mod:`tuner.orchestrate` does, and refuses a plan whose gangs
        split a link group.

        Acknowledging is per-session and cannot be undone, which is deliberate:
        a run that turns the guard back on halfway has already done whatever
        the guard was for.
        """
        self._acknowledged |= frozenset(outputs)

    def check(self, frame: Frame) -> None:
        """Raise :class:`TxRefused` unless ``frame`` may be sent."""
        kind = int(frame.frame_type)
        if kind == FrameType.READ:
            self._check_read(frame)
            return
        if kind == FrameType.WRITE:
            self._check_write(frame)
            return
        raise TxRefused(
            f"frame_type 0x{kind:02X} is a device response, not a request; "
            f"only READ (0xA2) and WRITE (0xA1) may be transmitted"
        )

    def note_sent(self, frame: Frame) -> None:
        """Record a frame that was actually transmitted.

        Separate from :meth:`check` so a caller can validate without spending
        budget, and so the count reflects the wire rather than intent.
        """
        if int(frame.frame_type) != FrameType.WRITE:
            return
        self.writes += 1
        if int(frame.user_id) != PRESET_SLOT_WORKING_AREA:
            # A preset write lands in storage, not in the working area, so it
            # must not count against the channels-touched cap -- otherwise one
            # store would exhaust the budget for the tune it is protecting.
            self.preset_writes += 1
            return
        self.channels_written.add(int(frame.channel_id))

    # -- internals ---------------------------------------------------------

    def _check_read(self, frame: Frame) -> None:
        if frame.payload:
            raise TxRefused("read frames carry no payload; the app forces DataLen=0")

        dt, ch, did = int(frame.data_type), int(frame.channel_id), int(frame.data_id)
        if dt == DataType.SYSTEM:
            if ch not in SYSTEM_READ_CHANNELS:
                raise TxRefused(
                    f"no observed system read at channel_id {ch}. Observed: "
                    f"{sorted(SYSTEM_READ_CHANNELS)}"
                )
            if did != 0:
                raise TxRefused(f"system reads use data_id 0, got {did}")
            return

        if dt == DataType.OUTPUT_CHANNEL:
            self._require_output_channel(ch)
            self._require_working_area(frame, "read")
            if did not in READABLE_OUTPUT_IDS:
                raise TxRefused(f"output data_id {did} is not a readable block")
            return

        raise TxRefused(
            f"data_type {dt} was never observed. Only SYSTEM (9) and "
            f"OUTPUT_CHANNEL (4) have been seen; INPUT_CHANNEL (3) has not "
            f"appeared in any capture, in either direction"
        )

    def _check_write(self, frame: Frame) -> None:
        dt, ch, did = int(frame.data_type), int(frame.channel_id), int(frame.data_id)

        # The blast-radius write cap bounds the whole session, preset frames
        # included -- it exists to stop a runaway loop, and a loop storing
        # presets runs away just as fast as one writing blocks.
        if self.writes >= self.blast_radius.max_writes:
            raise TxRefused(
                f"blast radius: {self.writes} writes already sent this session, "
                f"cap is {self.blast_radius.max_writes}"
            )

        if dt == DataType.SYSTEM:
            self._check_preset_name_write(frame)
            return

        if dt != DataType.OUTPUT_CHANNEL:
            raise TxRefused(
                f"the only writes ever observed are DataType 4 "
                f"(OUTPUT_CHANNEL) and the preset-name write on DataType 9; "
                f"got {dt}"
            )

        self._require_output_channel(ch)
        if int(frame.user_id) != PRESET_SLOT_WORKING_AREA:
            # Storing a record into a slot. Raises unless it is exactly the
            # observed shape and presets are enabled. It does not consume the
            # per-channel blast radius: a store is inherently all eight
            # channels and touches no audio, so capping it at one channel
            # would forbid the operation rather than bound it.
            self._require_working_area(frame, "write")
            return

        if not self.allow_writes:
            raise TxRefused(
                "writes are not enabled on this policy. Enable them "
                "deliberately, and only with a verified snapshot on disk"
            )

        if did not in WRITABLE_OUTPUT_IDS:
            extra = ""
            if did in (34, 35):
                extra = (
                    " -- its meaning is contradicted between the decompiled app "
                    "and the device's own readback"
                )
            elif did == 119:
                extra = (
                    " -- 119 is the bulk *read* aggregate; a bulk write has "
                    "never been observed"
                )
            raise TxRefused(f"output data_id {did} may not be written{extra}")

        if len(frame.payload) != BLOCK_PAYLOAD_LEN:
            raise TxRefused(
                f"every observed write carries a whole {BLOCK_PAYLOAD_LEN}-byte "
                f"block; got {len(frame.payload)}. Partial writes revert every "
                f"field they omit -- read-modify-write instead"
            )

        if (
            self.refuse_linked_channels
            and ch in self._linked
            and ch not in self._acknowledged
        ):
            raise TxRefused(
                f"output {ch + 1} is in a link group. Measured 2026-08-09: the "
                f"device does **not** mirror, so this write changes exactly "
                f"this channel and leaves its partner behind -- and the model "
                f"the optimizer reasoned about had them as one. A link can "
                f"also be a mechanical constraint rather than a convenience "
                f"(outputs 7 and 8 are two subwoofers in one ported box), in "
                f"which case unmatching them is a hardware hazard. Write every "
                f"member with the same values and call acknowledge_gang()."
            )

        prospective = self.channels_written | {ch}
        if len(prospective) > self.blast_radius.max_channels:
            raise TxRefused(
                f"blast radius: this session may touch "
                f"{self.blast_radius.max_channels} channel(s); "
                f"{sorted(self.channels_written)} already written, {ch} requested"
            )

    @staticmethod
    def _require_output_channel(channel: int) -> None:
        if not 0 <= channel <= 7:
            raise TxRefused(f"output channel_id {channel} is outside 0-7")

    def _check_preset_name_write(self, frame: Frame) -> None:
        """The one system write ever observed: a preset name, as part of a store."""
        ch, did, slot = (
            int(frame.channel_id),
            int(frame.data_id),
            int(frame.user_id),
        )
        # Shape before permission, so a frame that is not a preset name at all
        # -- a RESET_MCU at channel 96, say -- is refused for what it is rather
        # than for a flag being off.
        if ch != 0 or did != 0:
            raise TxRefused(
                f"the only writes ever observed are DataType 4 blocks and the "
                f"preset name at DataType 9 channel_id 0 / data_id 0; "
                f"got system {ch}/{did}"
            )
        if not self.allow_presets:
            raise TxRefused(
                "preset operations are not enabled on this policy. A recall "
                "replaces all eight channels at once; enable it deliberately"
            )
        if not 1 <= slot <= PRESET_SLOT_MAX:
            raise TxRefused(
                f"preset slot {slot} is outside 1-{PRESET_SLOT_MAX}. Slots "
                f"7-15 answer a name read from a stale buffer and store nothing"
            )
        if len(frame.payload) != PRESET_NAME_WRITE_LEN:
            raise TxRefused(
                f"preset name write carries {len(frame.payload)} bytes; the "
                f"observed write is exactly {PRESET_NAME_WRITE_LEN}"
            )

    def _is_permitted_preset_op(self, frame: Frame, verb: str) -> bool:
        """Is this the exact frame shape a preset store or recall uses?

        Deliberately narrow. ``user_id`` set is only ever legitimate on
        ``data_id`` 0, on a real slot, with the exact payload length the
        capture shows -- 0 for the recall read, a whole 296-byte record for the
        store write. Anything else with a slot selected is a frame nobody has
        observed, addressing storage, and is refused whatever the flag says.
        """
        if not self.allow_presets:
            return False
        if int(frame.data_id) != 0:
            return False
        if not 1 <= int(frame.user_id) <= PRESET_SLOT_MAX:
            return False
        if verb == "read":
            return not frame.payload
        return len(frame.payload) == OUTPUT_BULK_LEN

    def _require_working_area(self, frame: Frame, verb: str) -> None:
        """Refuse any output frame that addresses a preset slot.

        **This closes a hole that existed because reads were assumed safe.**
        Measured 2026-08-09: on an ``OUTPUT_CHANNEL`` frame, ``user_id`` selects
        a preset slot, and ``data_id`` 0 addresses the whole 296-byte channel
        record. The vendor app performs a preset **recall** by *reading*
        ``data_id`` 0 on channels 0-7 with ``user_id`` set -- there is no
        separate select opcode, and no write is involved. The device replies
        with the slot's contents *and* loads them over the working area.

        So a frame that looks in every respect like an ordinary read is the
        most destructive operation in the protocol short of ``RESET_MCU``: it
        silently replaces the entire live tune, on a device with no undo. The
        app's own behaviour says it knows -- it mutes the master volume either
        side of the eight reads, which nobody does for a read.

        The mirror of it, ``user_id`` set on a *write*, stores into the slot.
        Both are real capabilities and both belong behind an explicit preset
        API rather than reachable by leaving one header field non-zero.
        """
        uid = int(frame.user_id)
        if uid == PRESET_SLOT_WORKING_AREA:
            return
        if self._is_permitted_preset_op(frame, verb):
            return
        recall = verb == "read" and int(frame.data_id) == 0
        detail = (
            " -- that frame IS a preset recall: the device would load slot "
            f"{uid} over the entire working area, all eight channels, with no "
            "undo"
            if recall
            else f" -- that would address stored preset slot {uid}, not the live tune"
        )
        raise TxRefused(
            f"output {verb} carries user_id={uid}{detail}. Use the preset API "
            f"deliberately; do not reach it by leaving a header field set."
        )
