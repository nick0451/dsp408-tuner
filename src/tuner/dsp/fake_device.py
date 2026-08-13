"""A DSP-408 that lives in process, so the stack can be driven with no hardware.

Not a mock. It holds real state -- eight 296-byte channel records -- applies
writes to it, and answers reads from it, so a bug in read-modify-write shows up
here as wrong bytes rather than as an unmet expectation.

Everything it does was measured from ``captures/btsnoop_hci.log``: the
lock-step discipline, the ack codes, the bit-exact header echo, the 20-byte
zero-padded chunking, the connect ritual's replies, and the persistence model
where a power cycle preserves and a preset recall destroys.

**It fails loudly rather than improvising.** A request it does not recognise
raises :class:`UnexpectedRequest` instead of returning a plausible reply. A
fake that improvises trains the session layer against responses no device
produces, and the error surfaces later, on hardware, as something inexplicable.

Fault injection lives here too -- see :class:`Faults`. The error path is the
one part of the session layer that cannot be validated against the capture,
because the device never once returned ``0x52``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .framing import FrameReader, chunk_20
from .protocol import (
    BLOCK_PAYLOAD_LEN,
    Ack,
    DataType,
    Frame,
    FrameType,
)
from .txpolicy import SYSTEM_READ_CHANNELS

#: Whole-channel record: 37 blocks of 8 bytes.
RECORD_LEN = 296
BLOCKS_PER_RECORD = 37

#: ``data_id`` that reads a whole channel. Read-only: a bulk write has never
#: been observed and must not be invented here, or the session layer would be
#: developed against a capability the device may not have.
BULK_READ_DATA_ID = 119

N_OUTPUTS = 8

#: Slots the device actually stores. **Six, not fifteen** -- measured
#: 2026-08-09; see ``PRESET_SLOT_MAX`` in ``protocol.py`` for the evidence.
N_REAL_PRESETS = 6

#: Slots the app *reads names from* on connect. Reads of 7-15 are answered, but
#: with a stale buffer rather than storage, so the fake answers them too --
#: refusing would make the handshake diverge from the capture.
N_PRESETS = 15

#: ``data_id`` addressing a whole channel record inside a preset slot. Also EQ
#: band 0 when the payload is 8 bytes and ``user_id`` is 0. The overloading is
#: the device's, not ours.
PRESET_RECORD_DATA_ID = 0

#: A preset name write carries one byte more than a read returns. Meaning
#: unknown; reproduced so a round-trip through the fake matches the wire.
PRESET_NAME_WRITE_LEN = 16

#: Replies the real device gave during the connect ritual, verbatim. Channels
#: whose meaning is unknown are reproduced byte for byte and left unnamed.
SYSTEM_REPLIES: dict[int, bytes] = {
    2: bytes.fromhex("0100010000000000"),
    3: bytes(14) + b"\x01",
    4: b"MYDW-AV1.06",
    5: bytes.fromhex("2e00003200320100"),
    6: bytes.fromhex("0309040a0f121617"),
    7: b"",
    8: b"",
    19: bytes.fromhex("83838383d1c7bbbbbbbb"),
}

#: Preset names as read from the device, slots 1-15.
PRESET_NAMES: tuple[str, ...] = (
    "re-timed",
    "rockkkkkk",
    "- bass",
    "lbass",
    "test",
    "basssss++++",
    "lbass",
    "lbass",
    "lbass",
    "lbass",
    "lbass",
    "lbass",
    "lbass",
    "lbass",
    "lbass",
)

PRESET_NAME_LEN = 15
CURRENT_PRESET_CHANNEL = 52
PRESET_NAME_CHANNEL = 0


class FakeDeviceError(AssertionError):
    """Base for faults the fake refuses to paper over."""


class UnexpectedRequest(FakeDeviceError):
    """A request the real device was never observed answering.

    An ``AssertionError`` on purpose: this is a bug in the caller, surfaced at
    the point it happens, not a condition to be handled.
    """


class ProtocolViolationByHost(FakeDeviceError):
    """The host broke a rule the real link enforces -- e.g. pipelining."""


@dataclass
class Faults:
    """Failure modes to inject. All default to off."""

    #: Emit ``0x52`` instead of a normal reply. The real meaning of an error
    #: reply is unknown -- it was never observed -- so tests built on this must
    #: assert that we *abort*, never that we interpret it.
    reply_error: bool = False

    #: Send no reply at all, so the session's timeout path runs.
    drop_replies: int = 0

    #: Corrupt one echoed header field, to catch a matcher that relies on
    #: ordering rather than on the echoed tuple.
    broken_echo: bool = False

    #: Corrupt the checksum of the next reply.
    corrupt_checksum: int = 0

    #: Bytes to inject before the next reply, to exercise resync.
    garbage_prefix: bytes = b""

    #: Stop padding replies to 20 bytes, to prove the reader does not depend
    #: on it.
    no_chunking: bool = False


@dataclass
class DeviceImage:
    """Non-volatile state: eight channel records plus preset slots."""

    channels: list[bytearray] = field(default_factory=list)
    presets: list[bytes] = field(default_factory=list)
    preset_names: list[str] = field(default_factory=list)
    current_preset: int = 4

    #: ``DataType 9`` blocks, seeded from the capture. **Per-instance, not a
    #: module constant**, because channel 5 turned out to be the master volume
    #: (measured 2026-08-09) and a global that can move is state, not a
    #: reply table. Modelling it as a constant made it impossible to write a
    #: test for the one drift that provenance would otherwise miss.
    system: dict[int, bytes] = field(default_factory=lambda: dict(SYSTEM_REPLIES))

    @classmethod
    def flat(cls) -> DeviceImage:
        """A synthetic image, distinct per channel so mix-ups are visible."""
        channels = []
        for ch in range(N_OUTPUTS):
            rec = bytearray(RECORD_LEN)
            for band in range(31):
                freq = 100 + band * 100 + ch
                rec[band * 8 : band * 8 + 8] = bytes(
                    [freq & 0xFF, freq >> 8, 0x58, 0x02, 25, 0, 0, 0]
                )
            rec[248:256] = bytes([1, 1, 0xF4, 0x01, 0, 0, 0, 1])  # MISC, gain 500
            rec[256:264] = bytes([0xC2, 0x01, 0, 1, 0xAC, 0x0D, 0, 1])  # XOVER
            rec[264:272] = bytes([80, 0, 80, 0, 0, 0, 0, 0])  # MIX
            rec[272:280] = bytes([0xA4, 0x01, 0x38, 0, 0xF4, 0x01, 0, 0])
            rec[280:288] = bytes([0xA4, 0x01, 0x38, 0, 0xF4, 0x01, 0, 0])
            rec[288:296] = b"       \x00"
            channels.append(rec)
        return cls(
            channels=channels,
            presets=[b"".join(bytes(c) for c in channels)] * N_REAL_PRESETS,
            preset_names=list(PRESET_NAMES),
        )

    @classmethod
    def from_records(cls, records: list[bytes]) -> DeviceImage:
        """Build from eight real 296-byte records."""
        if len(records) != N_OUTPUTS:
            raise ValueError(f"need {N_OUTPUTS} records, got {len(records)}")
        for i, rec in enumerate(records):
            if len(rec) != RECORD_LEN:
                raise ValueError(f"record {i} is {len(rec)} bytes, need {RECORD_LEN}")
        channels = [bytearray(r) for r in records]
        return cls(
            channels=channels,
            presets=[b"".join(bytes(c) for c in channels)] * N_REAL_PRESETS,
            preset_names=list(PRESET_NAMES),
        )

    def snapshot(self) -> list[bytes]:
        return [bytes(c) for c in self.channels]


class FakeDsp408:
    """An in-process DSP-408.

    Drive it through :class:`~tuner.dsp.transport.LoopbackTransport`; the
    session layer cannot tell the difference from a socket.
    """

    def __init__(
        self,
        image: DeviceImage | None = None,
        faults: Faults | None = None,
        strict_lock_step: bool = True,
    ) -> None:
        self.image = image or DeviceImage.flat()
        self.faults = faults or Faults()
        self.strict_lock_step = strict_lock_step

        self._reader = FrameReader()
        self.received: list[Frame] = []
        self.writes: list[Frame] = []
        #: Slots recalled over the wire, in order. A test asserting a recall
        #: happened should check this rather than the resulting state, so a
        #: recall that loaded the slot already in the working area still counts.
        self.recalls: list[int] = []
        self.connected = False
        self._undrained_replies = 0
        self.lock_step_violations = 0

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        self.connected = True
        self._reader.reset()
        self._undrained_replies = 0

    def note_reply_read(self) -> None:
        """Called by the transport when the host drains the receive buffer.

        This is how lock-step is policed. Without it the fake cannot tell a
        host that waits for each reply from one that fires off two requests
        and reads both answers later -- and the real link only tolerates the
        first.
        """
        self._undrained_replies = 0

    def disconnect(self) -> None:
        self.connected = False

    def power_cycle(self) -> None:
        """Pull power and restore it.

        **Preserves everything.** Measured 2026-08-08: parameter writes are
        immediately non-volatile and come back byte-identical. The intuition
        that a reboot reverts uncommitted changes is exactly backwards here.
        """
        self.disconnect()
        self._reader.reset()

    def recall_preset(self, slot: int) -> None:
        """Load a preset into the working area, destroying what was there.

        This -- not power loss -- is what destroys an edit. Recalling does not
        modify the preset itself, which is what makes a preset slot the one
        restore point that survives everything.
        """
        self._require_real_slot(slot)
        blob = self.image.presets[slot - 1]
        self.image.channels = [
            bytearray(blob[i * RECORD_LEN : (i + 1) * RECORD_LEN])
            for i in range(N_OUTPUTS)
        ]
        self.image.current_preset = slot

    def store_preset(self, slot: int) -> None:
        """Copy the working area into a preset slot."""
        self._require_real_slot(slot)
        self.image.presets[slot - 1] = b"".join(bytes(c) for c in self.image.channels)

    @staticmethod
    def _require_real_slot(slot: int) -> None:
        if not 1 <= slot <= N_REAL_PRESETS:
            raise ValueError(
                f"preset slot {slot} outside 1-{N_REAL_PRESETS}. Slots 7-15 "
                f"answer a name read but are not storage -- they returned one "
                f"identical stale string across two captures."
            )

    # -- the wire ----------------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        """Consume host bytes, return whatever the device would send back."""
        if not self.connected:
            raise FakeDeviceError("host wrote to a disconnected device")

        out = bytearray()
        for received in self._reader.feed(data):
            out += self._handle(received.frame)
        return bytes(out)

    def _handle(self, frame: Frame) -> bytes:
        if self._undrained_replies:
            self.lock_step_violations += 1
            if self.strict_lock_step:
                raise ProtocolViolationByHost(
                    f"request {len(self.received) + 1} arrived while "
                    f"{self._undrained_replies} reply(s) were still unread. "
                    f"The real link is strictly lock-step: 2916 of 2916 "
                    f"captured transactions are one request, then one reply, "
                    f"read before the next request goes out."
                )
        self.received.append(frame)

        if frame.destructive:
            raise UnexpectedRequest(
                f"host sent {frame.destructive}. Nothing in the capture ever "
                f"did, and this fake will not pretend to survive it."
            )

        kind = int(frame.frame_type)
        if kind == FrameType.READ:
            payload = self._read(frame)
            ack = Ack.DATA
        elif kind == FrameType.WRITE:
            self._write(frame)
            payload = b""
            ack = Ack.RIGHT
        else:
            raise UnexpectedRequest(
                f"frame_type 0x{kind:02X} is a device response, not a request"
            )

        if self.faults.reply_error:
            ack, payload = Ack.ERROR, b""
        if self.faults.drop_replies > 0:
            self.faults.drop_replies -= 1
            return b""

        self._undrained_replies += 1
        return self._emit(frame, ack, payload)

    def _emit(self, request: Frame, ack: int, payload: bytes) -> bytes:
        reply = replace(request, frame_type=ack, payload=payload)
        if self.faults.broken_echo:
            reply = replace(reply, user_id=(int(request.user_id) + 1) & 0xFF)

        raw = bytearray(reply.encode())
        if self.faults.corrupt_checksum > 0:
            self.faults.corrupt_checksum -= 1
            raw[-2] ^= 0xFF

        out = bytearray(self.faults.garbage_prefix)
        self.faults.garbage_prefix = b""
        if self.faults.no_chunking:
            out += raw
        else:
            for piece in chunk_20(bytes(raw)):
                out += piece
        return bytes(out)

    # -- state -------------------------------------------------------------

    def _read(self, frame: Frame) -> bytes:
        dt, ch, did = int(frame.data_type), int(frame.channel_id), int(frame.data_id)

        if dt == DataType.SYSTEM:
            if ch == PRESET_NAME_CHANNEL:
                slot = int(frame.user_id)
                if not 1 <= slot <= N_PRESETS:
                    raise UnexpectedRequest(
                        f"preset name read with user_id {slot}; the app reads "
                        f"1-{N_PRESETS}"
                    )
                name = self.image.preset_names[slot - 1].encode("ascii")
                return name.ljust(PRESET_NAME_LEN, b"\x00")
            if ch == CURRENT_PRESET_CHANNEL:
                return bytes([self.image.current_preset])
            if ch in self.image.system:
                return self.image.system[ch]
            raise UnexpectedRequest(
                f"system read at channel_id {ch}. The app only ever read "
                f"{sorted(SYSTEM_READ_CHANNELS)}"
            )

        if dt == DataType.OUTPUT_CHANNEL:
            self._require_output(ch)
            slot = int(frame.user_id)
            if slot:
                # **A read that mutates.** Measured 2026-08-09: the app recalls
                # a preset purely by reading data_id 0 on channels 0-7 with
                # user_id set. No select opcode, no write. The device answers
                # with the slot's contents *and* loads them over the working
                # area, all eight channels.
                #
                # Modelled here rather than treated as a read, because the
                # whole point of this fake is that code developed against it
                # meets no surprises on the real unit -- and "the read wiped
                # the tune" is the worst possible surprise.
                if did != PRESET_RECORD_DATA_ID:
                    raise UnexpectedRequest(
                        f"slot-addressed read at data_id {did}; the app only "
                        f"ever reads {PRESET_RECORD_DATA_ID} with a user_id"
                    )
                self._require_real_slot(slot)
                self.recall_preset(slot)
                self.recalls.append(slot)
                return bytes(self.image.channels[ch])
            if did == BULK_READ_DATA_ID:
                return bytes(self.image.channels[ch])
            if 0 <= did < BLOCKS_PER_RECORD:
                return bytes(self.image.channels[ch][did * 8 : did * 8 + 8])
            raise UnexpectedRequest(f"output read at data_id {did}")

        raise UnexpectedRequest(
            f"read with data_type {dt}. Only SYSTEM (9) and OUTPUT_CHANNEL (4) "
            f"were ever observed; INPUT_CHANNEL (3) has never appeared."
        )

    def _write(self, frame: Frame) -> None:
        dt, ch, did = int(frame.data_type), int(frame.channel_id), int(frame.data_id)
        slot = int(frame.user_id)

        if dt == DataType.SYSTEM:
            # The only system write observed is a preset name, and only as the
            # first frame of a store.
            if ch != PRESET_NAME_CHANNEL or not slot:
                raise UnexpectedRequest(
                    f"system write at channel_id {ch} user_id {slot}; the only "
                    f"one observed is a preset name at channel "
                    f"{PRESET_NAME_CHANNEL} with a slot selected"
                )
            self._require_real_slot(slot)
            if len(frame.payload) != PRESET_NAME_WRITE_LEN:
                raise UnexpectedRequest(
                    f"preset name write carries {len(frame.payload)} bytes; "
                    f"the observed write is {PRESET_NAME_WRITE_LEN}"
                )
            name = frame.payload[:PRESET_NAME_LEN]
            self.image.preset_names[slot - 1] = name.split(b"\x00")[0].decode(
                "ascii", "replace"
            )
            self.writes.append(frame)
            return

        if dt != DataType.OUTPUT_CHANNEL:
            raise UnexpectedRequest(
                f"write with data_type {dt}; every observed write is DataType 4"
            )
        self._require_output(ch)

        if slot:
            # Storing one channel's record into a slot. The working area is
            # untouched -- which is exactly why a store is the safe half of the
            # preset pair and a recall is the dangerous half.
            if did != PRESET_RECORD_DATA_ID:
                raise UnexpectedRequest(
                    f"slot-addressed write at data_id {did}; the app only ever "
                    f"writes {PRESET_RECORD_DATA_ID} with a user_id"
                )
            self._require_real_slot(slot)
            if len(frame.payload) != RECORD_LEN:
                raise UnexpectedRequest(
                    f"slot-addressed write carries {len(frame.payload)} bytes; "
                    f"a whole channel record is {RECORD_LEN}"
                )
            blob = bytearray(self.image.presets[slot - 1])
            blob[ch * RECORD_LEN : (ch + 1) * RECORD_LEN] = frame.payload
            self.image.presets[slot - 1] = bytes(blob)
            self.writes.append(frame)
            return

        if did == BULK_READ_DATA_ID:
            raise UnexpectedRequest(
                "bulk write to data_id 119 was never observed. If the real "
                "device accepts it, that is a discovery to make deliberately, "
                "not a capability to assume here."
            )
        if not 0 <= did < BLOCKS_PER_RECORD:
            raise UnexpectedRequest(f"write to data_id {did}")
        if len(frame.payload) != BLOCK_PAYLOAD_LEN:
            raise UnexpectedRequest(
                f"write carries {len(frame.payload)} bytes; every observed "
                f"write is a whole {BLOCK_PAYLOAD_LEN}-byte block"
            )

        # A write replaces all eight bytes. Modelled explicitly, because a
        # fake that merged fields would hide the read-modify-write bug this
        # exists to catch.
        self.image.channels[ch][did * 8 : did * 8 + 8] = frame.payload
        self.writes.append(frame)

    @staticmethod
    def _require_output(channel: int) -> None:
        if not 0 <= channel < N_OUTPUTS:
            raise UnexpectedRequest(f"output channel_id {channel} outside 0-7")
