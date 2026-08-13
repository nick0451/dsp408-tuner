"""DSP-408 wire protocol: frame encoding and decoding.

Derived from the vendor Android app (`DataOptUtil.SendDataToDevice`); the full
derivation and evidence are in `docs/dsp408-protocol.md`. This module is pure
logic with no I/O, so it is testable without hardware and is where the wire
format is pinned down once.

Frame layout, all multi-byte fields little-endian::

    offset  size  field
      0..2    3   0x80 0x80 0x80   vendor head
         3    1   0xEE             frame start
         4    1   frame_type       0xA1 write, 0xA2 read
         5    1   device_id
         6    1   user_id
         7    1   data_type
         8    1   channel_id
         9    1   data_id
        10    1   bluetooth_device_id
        11    1   pc_custom
     12..13    2   payload length (uint16)
     14..      N   payload
      14+N    1   checksum
      15+N    1   0xAA             frame end

Total length is always ``len(payload) + 16``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

VENDOR_HEAD = bytes([0x80, 0x80, 0x80])
FRAME_START = 0xEE
FRAME_END = 0xAA

#: Bytes of framing overhead around the payload. The vendor spells this
#: ``CMD_LENGHT``; it is the total frame length minus the payload.
OVERHEAD = 16

#: Offset at which the payload begins.
HEADER_LEN = 14


class FrameType(IntEnum):
    WRITE = 0xA1
    READ = 0xA2


class Ack(IntEnum):
    """Response codes the device returns."""

    RIGHT = 0x51
    ERROR = 0x52
    DATA = 0x53


#: Frame types a *host* sends. The zero-checksum refusal in :meth:`Frame.encode`
#: applies only to these, because it is a rule of the vendor app's send path and
#: the device demonstrably does not follow it.
_HOST_FRAME_TYPES = frozenset({int(FrameType.WRITE), int(FrameType.READ)})


class DataType(IntEnum):
    """Known ``data_type`` values. The map is incomplete -- see the protocol doc."""

    INPUT_CHANNEL = 3
    OUTPUT_CHANNEL = 4
    SYSTEM = 9


#: (data_type, channel_id) pairs that trigger destructive device operations.
#: These sit among ordinary parameter ChannelIDs, three values above legitimate
#: ones, which is precisely why blind probing of this protocol was refused.
DESTRUCTIVE_COMMANDS: dict[tuple[int, int], str] = {
    (DataType.SYSTEM, 96): "RESET_MCU",
    (DataType.SYSTEM, 97): "TRANSMITTAL",
    (DataType.SYSTEM, 98): "RESET_GROUP_DATA",
}


class ProtocolError(ValueError):
    """Raised when bytes on the wire do not form a valid frame."""


class ChecksumError(ProtocolError):
    """Raised when a received frame's checksum does not match its contents."""


class UnsendableFrame(ValueError):
    """Raised when a frame is well-formed but the device will not accept it.

    The only known case is a checksum that computes to zero: the vendor app
    treats that as an error and refuses to transmit, so the device presumably
    rejects it too. Some parameter combinations are simply unsendable as a
    single frame.
    """


class DestructiveCommand(RuntimeError):
    """Raised when encoding a command that resets or wipes the device."""


def checksum(frame: bytes | bytearray, payload_len: int) -> int:
    """XOR of the frame from ``frame_type`` through the last payload byte.

    Covers offsets 4 to ``13 + payload_len`` inclusive.
    """
    value = 0
    for byte in frame[4 : HEADER_LEN + payload_len]:
        value ^= byte
    return value


@dataclass(frozen=True)
class Frame:
    """One protocol frame.

    Attributes mirror the vendor's field names so the decompiled source stays
    readable alongside this code.
    """

    frame_type: FrameType
    data_type: int
    channel_id: int
    data_id: int = 0
    device_id: int = 1
    #: **Preset slot selector, and the most dangerous field in the header.**
    #: 0 addresses the live working area; 1-6 address a stored preset. On an
    #: ``OUTPUT_CHANNEL`` frame with ``data_id`` 0 it turns an ordinary-looking
    #: request into a whole-device preset operation -- a *read* becomes a
    #: recall that overwrites the working tune. Measured 2026-08-09; see
    #: ``PRESET_SLOT_MAX`` and ``docs/dsp408-protocol.md``. Leave it at 0
    #: unless a preset operation is what you mean.
    user_id: int = 0
    bluetooth_device_id: int = 0
    pc_custom: int = 0
    payload: bytes = b""

    def __post_init__(self) -> None:
        for name in (
            "frame_type",
            "data_type",
            "channel_id",
            "data_id",
            "device_id",
            "user_id",
            "bluetooth_device_id",
            "pc_custom",
        ):
            value = getattr(self, name)
            if not 0 <= int(value) <= 0xFF:
                raise ValueError(f"{name}={value} does not fit in one byte")
        if len(self.payload) > 0xFFFF:
            raise ValueError("payload exceeds the 16-bit length field")
        if self.frame_type == FrameType.READ and self.payload:
            raise ValueError(
                "read frames carry no payload; the vendor app forces DataLen=0"
            )

    @property
    def destructive(self) -> str | None:
        """Name of the destructive operation this frame triggers, if any."""
        return DESTRUCTIVE_COMMANDS.get((int(self.data_type), int(self.channel_id)))

    def encode(self, allow_destructive: bool = False) -> bytes:
        """Serialize to wire bytes.

        Raises:
            DestructiveCommand: If this frame resets or wipes the device and
                ``allow_destructive`` was not explicitly set.
            UnsendableFrame: If the checksum computes to zero.
        """
        name = self.destructive
        if name is not None and not allow_destructive:
            raise DestructiveCommand(
                f"frame is {name} (data_type={int(self.data_type)}, "
                f"channel_id={int(self.channel_id)}), which resets or wipes the "
                f"device. Pass allow_destructive=True only if that is genuinely "
                f"intended."
            )

        n = len(self.payload)
        buf = bytearray(n + OVERHEAD)
        buf[0:3] = VENDOR_HEAD
        buf[3] = FRAME_START
        buf[4] = int(self.frame_type)
        buf[5] = int(self.device_id)
        buf[6] = int(self.user_id)
        buf[7] = int(self.data_type)
        buf[8] = int(self.channel_id)
        buf[9] = int(self.data_id)
        buf[10] = int(self.bluetooth_device_id)
        buf[11] = int(self.pc_custom)
        buf[12] = n & 0xFF
        buf[13] = (n >> 8) & 0xFF
        buf[HEADER_LEN : HEADER_LEN + n] = self.payload

        cs = checksum(buf, n)
        if cs == 0 and int(self.frame_type) in _HOST_FRAME_TYPES:
            # **A send-side rule of the vendor app, not a protocol invariant.**
            #
            # Corrected 2026-08-09. The refusal was applied to every frame, and
            # the second capture disproves that for the device direction: one
            # reply among 28 991 frames -- a preset-recall record, channel 3,
            # slot 6 -- carries a zero checksum, and our own reader parsed it
            # without complaint. So the device does emit them.
            #
            # It stays enforced for host->device frames, where the evidence is
            # what the app does: 0 of 2918 requests in the first capture and 0
            # in the second. That is where the guard protects us; extending it
            # to replies only made the in-process fake refuse to answer reads
            # the real unit answers, which is a fake that lies in the safe
            # direction right up until you trust it.
            raise UnsendableFrame(
                "checksum computes to zero, which the vendor app treats as an "
                "error and refuses to send; this parameter combination cannot "
                "be transmitted as a single frame"
            )
        buf[HEADER_LEN + n] = cs
        buf[HEADER_LEN + n + 1] = FRAME_END
        return bytes(buf)


def decode(data: bytes) -> Frame:
    """Parse wire bytes into a :class:`Frame`.

    Raises:
        ProtocolError: On bad framing or a truncated frame.
        ChecksumError: When the trailing checksum does not match.
    """
    if len(data) < OVERHEAD:
        raise ProtocolError(f"frame is {len(data)} bytes, minimum is {OVERHEAD}")
    if bytes(data[0:3]) != VENDOR_HEAD:
        raise ProtocolError(f"bad vendor head {bytes(data[0:3]).hex()}")
    if data[3] != FRAME_START:
        raise ProtocolError(f"bad frame start 0x{data[3]:02X}, expected 0xEE")

    n = data[12] | (data[13] << 8)
    if len(data) != n + OVERHEAD:
        raise ProtocolError(
            f"length field says {n} payload bytes (frame should be "
            f"{n + OVERHEAD}), got {len(data)}"
        )
    if data[HEADER_LEN + n + 1] != FRAME_END:
        raise ProtocolError(
            f"bad frame end 0x{data[HEADER_LEN + n + 1]:02X}, expected 0xAA"
        )

    expected = checksum(data, n)
    actual = data[HEADER_LEN + n]
    if actual != expected:
        raise ChecksumError(f"checksum 0x{actual:02X}, computed 0x{expected:02X}")

    return Frame(
        frame_type=FrameType(data[4]) if data[4] in iter(FrameType) else data[4],
        device_id=data[5],
        user_id=data[6],
        data_type=data[7],
        channel_id=data[8],
        data_id=data[9],
        bluetooth_device_id=data[10],
        pc_custom=data[11],
        payload=bytes(data[HEADER_LEN : HEADER_LEN + n]),
    )


@dataclass(frozen=True)
class EqBand:
    """One parametric EQ band as it appears on the wire.

    ``freq`` is **Hz**, 16-bit, 1 Hz resolution. It was read from the APK as an
    index into :data:`EQ_FREQ_TABLE_HZ`, which was wrong.

    Confirmed three ways, so this is no longer provisional: the vendor backup
    format stores the same field in Hz; the capture shows the app sending 486,
    2245, 2514 and 12699 Hz verbatim, none of which is a table entry; and a
    450 Hz crossover corner measured 449.4 Hz where snapping would have put it
    at 420. Corrected 2026-08-09.

    ``type`` selects the band's **shape** and is mapped as of 2026-08-09 --
    see :class:`EqBandType`. ``shf_db`` has still never been observed being
    written; a backend carries it through unchanged.

    .. warning::
       **A band whose ``type`` is not PEQ is not a peaking section, and
       ``bw`` does not mean what :func:`q_from_bw_raw` says it means.**
       ``optimize.biquad`` fits peaking biquads. Writing peaking parameters
       into a shelf band produces a device running a filter the optimizer never
       modelled -- and because both sides look entirely reasonable, the
       improvement invariant cannot see it. ``Dsp408Spp`` refuses rather than
       converting.

       This is why ``type`` reading 0 on all 112 channel-records was worth
       nothing as evidence: the corpus simply contained no shelves.
    """

    freq: int
    level: int
    bw: int
    shf_db: int = 0
    type: int = 0

    def encode(self) -> bytes:
        return bytes(
            [
                self.freq & 0xFF,
                (self.freq >> 8) & 0xFF,
                self.level & 0xFF,
                (self.level >> 8) & 0xFF,
                self.bw & 0xFF,
                (self.bw >> 8) & 0xFF,
                self.shf_db & 0xFF,
                self.type & 0xFF,
            ]
        )

    @classmethod
    def decode(cls, data: bytes) -> EqBand:
        if len(data) < 8:
            raise ProtocolError(f"EQ band needs 8 bytes, got {len(data)}")
        return cls(
            freq=data[0] | (data[1] << 8),
            level=data[2] | (data[3] << 8),
            bw=data[4] | (data[5] << 8),
            shf_db=data[6],
            type=data[7],
        )


#: Standard 1/3-octave centres. Originally read from the APK as the set of
#: frequencies a PEQ band may take.
#:
#: .. warning::
#:    **Probably not a device constraint.** A tune read back off the device
#:    uses 25 PEQ centres absent from this table, stored in Hz -- 8619, 5341,
#:    2514 and similar. This looks like the layout the app parks the 31 bands
#:    on before you move them, not a quantization grid. Do not treat it as one
#:    until the measurement in ``docs/STATE.md`` confirms the behaviour.
EQ_FREQ_TABLE_HZ: tuple[int, ...] = (
    20,
    25,
    32,
    40,
    50,
    63,
    80,
    100,
    125,
    160,
    200,
    250,
    315,
    400,
    500,
    630,
    800,
    1000,
    1250,
    1600,
    2000,
    2500,
    3150,
    4000,
    5000,
    6300,
    8000,
    10000,
    12500,
    16000,
    20000,
)

#: Crossover frequencies read from the APK. Subject to the same doubt as
#: :data:`EQ_FREQ_TABLE_HZ`: a tune read off the device uses 55, 450 and
#: 2500 Hz, none of which appear here.
XOVER_FREQ_TABLE_HZ: tuple[int, ...] = (
    20,
    23,
    27,
    32,
    37,
    42,
    49,
    57,
    66,
    76,
    88,
    102,
    118,
    137,
    162,
    187,
    216,
    250,
    289,
    334,
    375,
    420,
    486,
    561,
    648,
    749,
    866,
    1000,
    1123,
    1297,
    1498,
    1731,
    2000,
    2245,
    2594,
    2997,
    3462,
    4000,
    4757,
    5496,
    6350,
    7127,
    8000,
    9243,
    10679,
    12338,
    13849,
    15102,
    16000,
    17959,
    20000,
)


# ---------------------------------------------------------------------------
# Parameter scaling. Measured 2026-08-08 against the vendor app's own display
# via read-from-device backups; see docs/dsp408-protocol.md.
# ---------------------------------------------------------------------------

#: Raw value that means 0 dB, shared by output gain and PEQ band level.
UNITY_RAW = 600

#: Steps per dB for both fields.
RAW_PER_DB = 10

#: Bandwidth offset. ``bw_raw`` 0 is 0.05 octaves, not 0.
BW_OFFSET_RAW = 5

#: Bandwidth resolution, in raw steps per octave.
BW_RAW_PER_OCTAVE = 100


def gain_dbfs(gain_raw: int) -> float:
    """Convert a raw gain or PEQ level to dB.

    Exact on all eight channels of a real tune across raw values 433, 470,
    480 and 500, checked against the vendor app's display. The PEQ ``level``
    field uses the same encoding, so 600 is 0 dB in both.
    """
    return gain_raw / RAW_PER_DB - UNITY_RAW / RAW_PER_DB


def gain_raw_for(dbfs: float) -> int:
    """Raw value for a gain in dB. Inverse of :func:`gain_dbfs`."""
    return int(round(dbfs * RAW_PER_DB)) + UNITY_RAW


#: The stored bandwidth domain, measured 2026-08-12 from the vendor app's own
#: Q limits. The app reports Q ranging **0.404 to 28.852**, and those two
#: awkward decimals land exactly on ``q_from_bw_raw`` for raw 295 and raw 0 --
#: three decimal places at both ends, with the wide end falling on precisely
#: 3.000 octaves.
#:
#: That is a **third independent confirmation of ``octaves = (raw + 5)/100``**,
#: and the only one that pins the ``+5``. The offset is what puts the narrow
#: end at 0.05 octaves rather than 0, and a wrong offset shifts every
#: bandwidth by a constant -- which a fitter absorbs into neighbouring bands
#: and no listener ever hears. The other two routes are the app's displayed Q
#: and the measured half-gain widths at raw 25 / 65 / 134.
BW_RAW_MIN = 0
BW_RAW_MAX = 295

#: The same domain in octaves: 0.05 to 3.00.
BW_OCTAVES_MIN = 0.05
BW_OCTAVES_MAX = 3.00

#: And in Q, for reference. **These are the device's limits, not the fitter's**
#: -- ``FitConstraints`` deliberately stays well inside them.
DEVICE_Q_MAX = 28.852  # bw_raw 0
DEVICE_Q_MIN = 0.404  # bw_raw 295

#: EQ band gain range, measured 2026-08-12 from the app: symmetric +/-12 dB.
DEVICE_EQ_GAIN_DB = 12.0


def bandwidth_octaves(bw_raw: int) -> float:
    """PEQ bandwidth in octaves.

    The device stores bandwidth, not Q -- **this is the uniformly quantized
    parameter and the one an optimizer should search.** Q is a display
    convenience derived from it, and its resolution is wildly non-uniform: one
    raw step moves Q by 0.6 near Q=9.6 but by 0.002 near Q=0.5.
    """
    return (bw_raw + BW_OFFSET_RAW) / BW_RAW_PER_OCTAVE


def q_from_bw_raw(bw_raw: int) -> float:
    """Q the vendor app displays for a raw bandwidth.

    Standard peaking-filter relation, ``Q = sqrt(2**N) / (2**N - 1)``.
    Verified against five measured (bw_raw, displayed Q) pairs spanning
    Q 0.99 to 4.97, every one within the app's display rounding.
    """
    from math import log, sinh

    return 1.0 / (2.0 * sinh(log(2) / 2.0 * bandwidth_octaves(bw_raw)))


def bw_raw_for_q(q: float) -> int:
    """Raw bandwidth the vendor app stores for a requested Q.

    **Rounds bandwidth up**, so the achieved Q is never higher than requested
    -- a narrower filter than asked for is the more surprising failure, so the
    device errs wide. Observed on three requests: Q 1 stored 134, Q 3 stored
    43, Q 1.5 stored 90. The last discriminates the rule, since ordinary
    rounding would have given 89.
    """
    from math import asinh, ceil, log

    if q <= 0:
        raise ValueError("Q must be positive")
    octaves = asinh(1 / (2 * q)) / (log(2) / 2)
    return ceil(octaves * BW_RAW_PER_OCTAVE) - BW_OFFSET_RAW


def nearest_eq_index(freq_hz: float) -> int:
    """Index of the closest entry in :data:`EQ_FREQ_TABLE_HZ`.

    Chosen by log-frequency distance, because the table is logarithmically
    spaced and linear distance would bias every choice upward.

    **Not established as a quantizer.** See the warning on the table. This is
    currently only useful for describing where the app's default bands sit.
    """
    return _nearest_log_index(freq_hz, EQ_FREQ_TABLE_HZ)


def nearest_xover_index(freq_hz: float) -> int:
    """Index of the closest available crossover frequency."""
    return _nearest_log_index(freq_hz, XOVER_FREQ_TABLE_HZ)


def _nearest_log_index(freq_hz: float, table: tuple[int, ...]) -> int:
    if freq_hz <= 0:
        raise ValueError("frequency must be positive")
    from math import log

    target = log(freq_hz)
    return min(range(len(table)), key=lambda i: abs(log(table[i]) - target))


# ---------------------------------------------------------------------------
# Output channels (DataType 4)
#
# ChannelID selects the output; DataID selects which block of parameters the
# 8-byte payload carries. Derived from the `DataType == 4` branch of
# DataOptUtil.SendDataToDevice.
# ---------------------------------------------------------------------------

#: PEQ bands per output. DataID 0..30 address them individually.
OUTPUT_EQ_BANDS = 31

#: Payload size of every individual output parameter block.
BLOCK_PAYLOAD_LEN = 8

#: DataLen the vendor uses to write a whole output channel in one frame.
OUTPUT_BULK_LEN = 296


class EqBandType(IntEnum):
    """``EqBand.type``. Mapped 2026-08-09 by A/B in the vendor app.

    The app offers the low shelf only on band 1 and the high shelf only on
    band 10, which is where the two A/Bs were taken. **Whether the device would
    accept a shelf on any other band is unobserved** -- that is an app
    constraint until someone shows otherwise, and it must not be assumed to be
    a device one, nor assumed not to be.
    """

    PEQ = 0
    LOW_SHELF = 1
    HIGH_SHELF = 2


class XoverAlignment(IntEnum):
    """``h_filter``/``l_filter``. Mapped 2026-08-09 by A/B in the vendor app.

    ``DEFEAT`` is not an alignment -- it disables the crossover on that side.
    Setting it discards the corner frequency's effect entirely, which is a
    different act from moving the corner out of the way, and the optimizer
    should never reach for it by accident.
    """

    LINKWITZ_RILEY = 0
    BUTTERWORTH = 1
    BESSEL = 2
    DEFEAT = 3


#: Slopes the device can store, in dB/octave. The stored byte is the index.
XOVER_SLOPES_DB_OCT: tuple[int, ...] = (6, 12, 18, 24)


def slope_db_per_octave(level_raw: int) -> int:
    """``h_level``/``l_level`` -> dB per octave."""
    if not 0 <= level_raw < len(XOVER_SLOPES_DB_OCT):
        raise ValueError(
            f"crossover level {level_raw} is outside 0-"
            f"{len(XOVER_SLOPES_DB_OCT) - 1}; only {XOVER_SLOPES_DB_OCT} "
            f"dB/octave are storable"
        )
    return XOVER_SLOPES_DB_OCT[level_raw]


def level_raw_for_slope(db_per_octave: int) -> int:
    """dB per octave -> ``h_level``/``l_level``.

    Raises rather than rounding. A 15 dB/octave request is a caller error, and
    silently storing 12 or 18 would make the device disagree with the model the
    optimizer reasoned about -- the failure the improvement invariant is least
    able to see.
    """
    try:
        return XOVER_SLOPES_DB_OCT.index(int(db_per_octave))
    except ValueError:
        raise ValueError(
            f"{db_per_octave} dB/octave is not storable; the device offers "
            f"{XOVER_SLOPES_DB_OCT}"
        ) from None


#: Highest addressable preset slot. **Measured 2026-08-09.**
#:
#: The first capture showed the app reading fifteen preset names at
#: ``DataType 9 / ChannelID 0`` with ``user_id`` 1..15, and that was recorded as
#: fifteen slots. **It is six.** The second capture read all fifteen again, and
#: slots 7-15 returned one *identical* name that had changed between sessions --
#: previously ``lbass`` on all nine, now ``d1_lf27`` on all nine, which is the
#: filename of a ``.DDP`` saved from the Windows app hours earlier. Nine
#: identical names that track something else are a stale buffer, not storage.
#: The Android app exposes exactly six slots, which agrees.
PRESET_SLOT_MAX = 6

#: ``user_id`` addressing the live working area rather than a stored preset.
PRESET_SLOT_WORKING_AREA = 0

#: Length of a preset name as the device returns it. Writes carry one byte more
#: -- a trailing value whose meaning is unknown (0x26 in the only write
#: observed). Do not assume it is padding.
PRESET_NAME_LEN = 15
PRESET_NAME_WRITE_LEN = 16

#: The trailing byte on the one observed preset-name write. It is **not** a
#: checksum of the name (the bytes sum to 0xAE, not this), not the slot, and
#: not the length. Reproduced because the device saw it and we have no evidence
#: it is ignored -- sending something else is an experiment, not a default.
PRESET_NAME_TRAILER = 0x26


class OutputBlock(IntEnum):
    """``DataID`` values addressing non-EQ output blocks.

    DataID 0..30 are the EQ bands; these start above them.

    .. note::
       **Block 33 is confirmed as the input mixer, 2026-08-12.** One byte per
       input, 0-100, matching the vendor app's mixer grid exactly -- see
       :class:`OutputMix`. Only the first four bytes are meaningful on a
       four-input device; bytes 5-8 are zero on the live device and in all 40
       ``.DDP`` files.

    .. warning::
       **``MIX_IN_9_16`` is an APK reading that the device's own readback
       contradicts, and blocks 34/35 must not be written until it is settled.**

       Block 33 does look like a mix, and now *is* one: on channel 0 it reads
       ``50 00 50 00 00 00 00 00`` -- inputs 1 and 3 at 80 -- and channel 1
       reads ``00 50 00 50 ...``, the other side of a stereo pair. Block 34
       reads ``a4 01 38 00 f4 01 00 00`` on **every** channel, which decodes as
       :class:`OutputDynamics` (all_pass_q 420, attack 56, release 500) and
       looks nothing like a mix.

       **Confirming 33 makes 34's name more explicable, not less.** Block 33
       carries *eight* input slots on a device with *four* inputs, and the APK
       calls 34 "inputs 9-16". Both fit a shared codebase for a larger sibling
       product, on which this model simply leaves the upper half unwired --
       which is also why 34 never varies and why no vendor app exposes it.

       Block 34 is also not simply a copy of 35. They are byte-identical on
       channels 0-5 and differ on channels 6 and 7 -- in byte 7 only, the
       ``linkgroup_num``, which is set on exactly that pair. So 34 carries the
       same fields as 35 without the link group.

       This holds on two independent read paths: the RFCOMM bulk readback in
       ``captures/btsnoop_hci.log`` and every ``.DDP`` in the repository. It is
       also why :mod:`tuner.dsp.ddp` labels the pair "dynamics A" and
       "dynamics B" -- the two modules have disagreed since both were written.

       The name is left alone rather than corrected, because "not mix" is much
       better evidenced than any particular replacement, and renaming would
       assert one. A write to 34 sends bytes to an opcode whose destination is
       unverified, on a device with no undo.
    """

    MISC = 31
    XOVER = 32
    MIX_IN_1_8 = 33
    MIX_IN_9_16 = 34
    DYNAMICS = 35
    NAME = 36


#: Output ``DataID`` values no write path may target. See the warning on
#: :class:`OutputBlock`: their meaning is contradicted between the decompiled
#: app and the device's own readback, and the device has no undo.
UNVERIFIED_OUTPUT_BLOCKS = frozenset({34, 35})


def _require_block(data: bytes, what: str) -> None:
    if len(data) < BLOCK_PAYLOAD_LEN:
        raise ProtocolError(f"{what} needs {BLOCK_PAYLOAD_LEN} bytes, got {len(data)}")


@dataclass(frozen=True)
class OutputMisc:
    """Output level, delay and routing basics (DataID 31).

    ``gain_raw`` and ``delay_raw`` are device units. Both scalings are now
    measured -- ``dB = raw/10 - 60`` and integer samples at 48 kHz -- so the
    ``_raw`` suffixes are conservative rather than necessary. Renaming them is
    a pending deliberate refactor, not a drive-by.

    ``enabled`` is **1 when the channel is on and 0 when it is muted** --
    measured 2026-08-09, and note the sense, which is the opposite of the name
    the vendor sources give it.

    The APK calls this field ``mute``, and it was carried under that name here
    until an A/B settled it: muting output 1 in the vendor app changed
    **exactly one byte in the whole backup**, this one, 1 to 0
    (``eq_channel1_no_mute.DDP`` vs ``eq_channel1_mute.DDP``). It agrees with
    the survey that prompted the experiment -- the field reads 1 on 111 of the
    112 channel-records in the repository, the exception being a channel that
    was switched off.

    ``gain_raw`` was **unchanged** across that A/B, so muting is a real
    separate control rather than a gain zeroing, and a backend can set one
    without disturbing the other.

    ``spk_type`` is constant per channel across every backup (1, 2, 3, 7, 8,
    9, 15, 18 for outputs 1-8) and its meaning is unknown. ``polar`` is 0 or 1
    and is presumably polarity inversion, unconfirmed. ``eq_mode`` is 0
    everywhere. All three are carried through, not set.
    """

    #: 1 = channel on, 0 = muted. Named for what it does, not for what the
    #: decompiled app called it.
    enabled: int = 0
    polar: int = 0
    gain_raw: int = 0
    delay_raw: int = 0
    eq_mode: int = 0
    spk_type: int = 0

    def encode(self) -> bytes:
        return bytes(
            [
                self.enabled & 0xFF,
                self.polar & 0xFF,
                self.gain_raw & 0xFF,
                (self.gain_raw >> 8) & 0xFF,
                self.delay_raw & 0xFF,
                (self.delay_raw >> 8) & 0xFF,
                self.eq_mode & 0xFF,
                self.spk_type & 0xFF,
            ]
        )

    @classmethod
    def decode(cls, data: bytes) -> OutputMisc:
        _require_block(data, "OutputMisc")
        return cls(
            enabled=data[0],
            polar=data[1],
            gain_raw=data[2] | (data[3] << 8),
            delay_raw=data[4] | (data[5] << 8),
            eq_mode=data[6],
            spk_type=data[7],
        )


@dataclass(frozen=True)
class OutputMix:
    """How much of each input this output takes (DataID 33).

    **One byte per input, 0-100**, the same numbers the vendor app's mixer grid
    shows. Confirmed 2026-08-12 against screenshots of both the Windows and iOS
    mixers, and cross-checked against ``docs/hardware.md``'s reachability
    table -- which was derived months earlier by sweeping outputs on the bench
    and listening for silence, sharing no reasoning with this decode. The live
    device agrees with it exactly.

    **Eight slots on a four-input device.** Bytes 5-8 are zero on every channel
    of the live device and in all 40 ``.DDP`` files. The block is sized for a
    larger sibling product, which is also the most likely explanation for the
    APK naming block 34 ``MIX_IN_9_16``.

    Not part of :class:`~tuner.dsp.backend.ChannelConfig`, deliberately: the
    mixer is routing, not tuning, and nothing in the optimizer should be able
    to change which inputs feed a driver. It is read, carried through
    read-modify-write, and used to answer *which outputs can this stimulus
    reach* -- see :meth:`reaches`.
    """

    levels: tuple[int, ...]

    #: Inputs this hardware actually has. The block addresses eight.
    N_REAL_INPUTS = 4

    @classmethod
    def decode(cls, block: bytes) -> OutputMix:
        if len(block) != BLOCK_PAYLOAD_LEN:
            raise ProtocolError(
                f"mix block is {len(block)} bytes, expected {BLOCK_PAYLOAD_LEN}"
            )
        return cls(levels=tuple(block))

    def encode(self) -> bytes:
        if len(self.levels) != BLOCK_PAYLOAD_LEN:
            raise ProtocolError(
                f"mix has {len(self.levels)} levels, expected {BLOCK_PAYLOAD_LEN}"
            )
        return bytes(v & 0xFF for v in self.levels)

    @property
    def inputs(self) -> tuple[int, ...]:
        """The four levels that mean anything on this model."""
        return self.levels[: self.N_REAL_INPUTS]

    def reaches(self, input_index: int) -> bool:
        """Whether a signal on ``input_index`` (0-based) arrives at this output.

        The bench rig drives one input and cables one output at a time, and an
        output the stimulus never reaches measures **silence** -- which
        ``require_signal_response`` correctly rejects, after the sweep has
        already been run. Asking first is cheaper.
        """
        if not 0 <= input_index < self.N_REAL_INPUTS:
            raise ValueError(f"input {input_index} outside 0..{self.N_REAL_INPUTS - 1}")
        return self.levels[input_index] > 0


@dataclass(frozen=True)
class OutputXover:
    """High-pass and low-pass crossover settings (DataID 32).

    ``h_freq``/``l_freq`` are **frequencies in Hz**, 16-bit, 1 Hz resolution.

    .. note::
       This docstring previously said they were indices into
       :data:`XOVER_FREQ_TABLE_HZ`. **That was wrong**, and it was disproved
       three ways: a 450 Hz corner measured 449.4 Hz where the nearest table
       entry would have put it at 420; the app sent a hand-typed 1234 Hz
       verbatim; and 1234 appears in no table. Corrected 2026-08-09.

    **Both selector pairs are mapped as of 2026-08-09**, by fourteen
    single-control A/Bs in the vendor app. ``h_level``/``l_level`` are the
    slope, ``h_filter``/``l_filter`` the alignment, and the two are orthogonal
    -- every high-pass A/B left the low-pass bytes untouched and vice versa:

    ===== ============  ===== =================
    level slope         filter alignment
    ===== ============  ===== =================
    0     6 dB/octave   0      Linkwitz-Riley
    1     12 dB/octave  1      Butterworth
    2     18 dB/octave  2      Bessel
    3     24 dB/octave  3      Defeat (bypassed)
    ===== ============  ===== =================

    See :class:`XoverAlignment`, :func:`slope_db_per_octave` and
    :func:`level_raw_for_slope`.

    Why this looked unmappable for so long: ``h_filter``/``l_filter`` read 0 on
    all 112 channel-records because every tune ever saved used Linkwitz-Riley,
    and ``h_level``/``l_level`` only ever took 1 or 3 because the operator only
    ever used 12 and 24 dB/octave. Absence of variation in the corpus, not
    absence of meaning -- and no amount of staring at the existing backups would
    have produced this. Two minutes in the app did.

    .. note::
       This docstring previously said OUT5's 450 Hz low-pass carries
       ``l_level = 1`` and measured as textbook LR4, and offered that as
       evidence that 1 meant 24 dB/octave. **It conflated two tunes.** The
       configuration that was actually on the device for that measurement has
       ``l_filter = 0, l_level = 3`` -- Linkwitz-Riley, 24 dB/octave, precisely
       LR4. ``l_level = 1`` belongs to preset 4, which was never measured. So
       the acoustic result (450.1 Hz, 0.247 dB rms against an LR4 fit)
       *corroborates* this table rather than contradicting it.

    ``h_freq``/``l_freq`` are still carried through unchanged when the caller
    does not specify them; only the two selectors are now computable.
    """

    h_freq: int = 0
    h_filter: int = 0
    h_level: int = 0
    l_freq: int = 0
    l_filter: int = 0
    l_level: int = 0

    def encode(self) -> bytes:
        return bytes(
            [
                self.h_freq & 0xFF,
                (self.h_freq >> 8) & 0xFF,
                self.h_filter & 0xFF,
                self.h_level & 0xFF,
                self.l_freq & 0xFF,
                (self.l_freq >> 8) & 0xFF,
                self.l_filter & 0xFF,
                self.l_level & 0xFF,
            ]
        )

    @classmethod
    def decode(cls, data: bytes) -> OutputXover:
        _require_block(data, "OutputXover")
        return cls(
            h_freq=data[0] | (data[1] << 8),
            h_filter=data[2],
            h_level=data[3],
            l_freq=data[4] | (data[5] << 8),
            l_filter=data[6],
            l_level=data[7],
        )


@dataclass(frozen=True)
class OutputDynamics:
    """Compressor and channel-link settings (DataID 35). Units unconfirmed."""

    all_pass_q: int = 0
    attack_time: int = 0
    release_time: int = 0
    threshold: int = 0
    linkgroup_num: int = 0

    def encode(self) -> bytes:
        return bytes(
            [
                self.all_pass_q & 0xFF,
                (self.all_pass_q >> 8) & 0xFF,
                self.attack_time & 0xFF,
                (self.attack_time >> 8) & 0xFF,
                self.release_time & 0xFF,
                (self.release_time >> 8) & 0xFF,
                self.threshold & 0xFF,
                self.linkgroup_num & 0xFF,
            ]
        )

    @classmethod
    def decode(cls, data: bytes) -> OutputDynamics:
        _require_block(data, "OutputDynamics")
        return cls(
            all_pass_q=data[0] | (data[1] << 8),
            attack_time=data[2] | (data[3] << 8),
            release_time=data[4] | (data[5] << 8),
            threshold=data[6],
            linkgroup_num=data[7],
        )


def _output_frame(channel: int, data_id: int, payload: bytes) -> Frame:
    return Frame(
        frame_type=FrameType.WRITE,
        data_type=DataType.OUTPUT_CHANNEL,
        channel_id=channel,
        data_id=data_id,
        payload=payload,
    )


def write_output_eq(channel: int, band: int, values: EqBand) -> Frame:
    """Frame writing one PEQ band on one output."""
    if not 0 <= band < OUTPUT_EQ_BANDS:
        raise ValueError(f"band must be 0..{OUTPUT_EQ_BANDS - 1}, got {band}")
    return _output_frame(channel, band, values.encode())


def write_output_misc(channel: int, values: OutputMisc) -> Frame:
    """Frame writing mute, polarity, gain, delay, eq mode and speaker type."""
    return _output_frame(channel, OutputBlock.MISC, values.encode())


def write_output_xover(channel: int, values: OutputXover) -> Frame:
    """Frame writing the crossover block."""
    return _output_frame(channel, OutputBlock.XOVER, values.encode())


def write_output_dynamics(channel: int, values: OutputDynamics) -> Frame:
    """Frame writing the compressor and link-group block."""
    return _output_frame(channel, OutputBlock.DYNAMICS, values.encode())


def write_output_mix(channel: int, mix: list[int] | tuple[int, ...]) -> list[Frame]:
    """Frames setting this output's mix of the 16 inputs, one byte per input.

    Returns two frames -- inputs 1-8 and 9-16 -- because the vendor splits the
    matrix across two DataIDs. Short sequences are zero-padded, which **mutes**
    the unspecified inputs rather than leaving them untouched; pass the full
    vector if that is not what you want.
    """
    if len(mix) > 16:
        raise ValueError(f"at most 16 input levels, got {len(mix)}")
    padded = list(mix) + [0] * (16 - len(mix))
    return [
        _output_frame(
            channel, OutputBlock.MIX_IN_1_8, bytes(v & 0xFF for v in padded[:8])
        ),
        _output_frame(
            channel, OutputBlock.MIX_IN_9_16, bytes(v & 0xFF for v in padded[8:])
        ),
    ]


def read_output(channel: int, block: int) -> Frame:
    """Frame requesting one output block. Read frames carry no payload."""
    return Frame(
        frame_type=FrameType.READ,
        data_type=DataType.OUTPUT_CHANNEL,
        channel_id=channel,
        data_id=int(block),
    )
