"""Reader for the vendor app's ``.DDP`` tune backup format.

The Windows app (``DSP-408-Windows-V1.24``) saves a whole configuration as a
flat sequence of the same 8-byte parameter blocks the wire protocol carries.
That makes the file a **zero-risk readback channel**: change one control in the
vendor app, save a second backup, diff the two, and the raw encoding of that
control falls out without a single byte being written by us.

This is the same route that settled the delay question by reading an existing
tune rather than running the acoustic experiment that had been planned for it.

Pure logic, no I/O -- it takes bytes. Keep it that way; see the DSP backend
contract in CLAUDE.md.

Layout, derived from ``dspcartunebackups.DDP`` and cross-checked against the
block table in ``docs/dsp408-protocol.md``::

    offset 0        u8      length of the magic string (16)
    offset 1..16    ascii   "DSA-4.8_File_3.0"
    offset 17..48   ascii   preset name, NUL-padded to 32 bytes
    offset 49       -- 553 8-byte blocks follow --
      blocks   0..32    global / unidentified          (33 blocks)
      blocks  33..248   6 input records of 36 blocks
      blocks 249..544   8 output records of 37 blocks
      blocks 545..552   8 trailing name blocks

The **output** section is decoded with confidence: 37 blocks matches
``OUTPUT_BULK_LEN`` (296 bytes) exactly, and every field decodes to a coherent
value for a real three-way active tune. The **input and global** sections are
carried through verbatim as raw blocks -- the 36-block stride is solid, but
every input record in the only sample available is at its default, so nothing
pins the field layout and guessing it would assert something unmeasured.

**Frequencies in this file are in Hz, not table indices** -- the file stores
values such as 8619 Hz for PEQ centres and 450 Hz for crossover corners, none
of which appear in ``EQ_FREQ_TABLE_HZ`` or ``XOVER_FREQ_TABLE_HZ``. That was
once an open question between "the tables are only the app's default band
layout" and "the app quantizes on send". **Settled 2026-08-08 by measurement:**
a 450 Hz corner measures 449.4 Hz, so the device honours what it is given and
the tables are not constraints. See ``docs/dsp408-protocol.md``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .protocol import ProtocolError

#: Identifies the format. Present in the only sample available; other app
#: versions may write a different revision string.
MAGIC = b"DSA-4.8_File_3.0"

BLOCK_LEN = 8
NAME_LEN = 32
HEADER_LEN = 1 + len(MAGIC) + NAME_LEN

GLOBAL_BLOCKS = 33
INPUT_COUNT = 6
BLOCKS_PER_INPUT = 36
OUTPUT_COUNT = 8
BLOCKS_PER_OUTPUT = 37
TRAILING_BLOCKS = 8

INPUT_SECTION_START = GLOBAL_BLOCKS
OUTPUT_SECTION_START = INPUT_SECTION_START + INPUT_COUNT * BLOCKS_PER_INPUT
TRAILING_SECTION_START = OUTPUT_SECTION_START + OUTPUT_COUNT * BLOCKS_PER_OUTPUT
TOTAL_BLOCKS = TRAILING_SECTION_START + TRAILING_BLOCKS

#: PEQ bands per output record, before the six non-EQ blocks.
EQ_BANDS = 31


def _u16(block: bytes, offset: int) -> int:
    return block[offset] | (block[offset + 1] << 8)


@dataclass(frozen=True)
class DdpEqBand:
    """One PEQ band as the backup file stores it.

    ``level_raw`` and ``bw_raw`` are device units with unmeasured scaling and
    are named accordingly -- see the units table in CLAUDE.md. ``freq_hz`` is
    named for Hz because the stored values are unambiguously Hz; whether the
    *device* honours a value off the table is a separate, open question.
    """

    freq_hz: int
    level_raw: int
    bw_raw: int
    shf_db: int
    type: int

    @classmethod
    def decode(cls, block: bytes) -> DdpEqBand:
        return cls(
            freq_hz=_u16(block, 0),
            level_raw=_u16(block, 2),
            bw_raw=_u16(block, 4),
            shf_db=block[6],
            type=block[7],
        )


@dataclass(frozen=True)
class DdpOutput:
    """One output channel record (37 blocks).

    Field names follow ``protocol.OutputMisc`` and ``protocol.OutputXover`` so
    that the file and the wire share one vocabulary, with ``_hz`` added where
    the stored unit is known.

    ``delay_samples`` is named in engineering units because that scaling *has*
    been measured -- integer samples at 48 kHz, see ``docs/dsp408-protocol.md``.
    ``protocol.OutputMisc.delay_raw`` still carries the old name and should
    follow.

    ``enabled`` is **1 when the channel is on, 0 when muted** -- measured
    2026-08-09. Muting output 1 in the vendor app changed exactly this byte
    and nothing else in the whole file (``eq_channel1_no_mute.DDP`` vs
    ``eq_channel1_mute.DDP``), with ``gain_raw`` untouched.

    This module carried it as ``byte0`` for months rather than adopting the
    APK's name for it, because it read 1 on seven of the eight channels of a
    working tune and 0 only on the one that was switched off -- backwards for
    something called ``mute``. Declining to guess was right, and the guess it
    declined to make turns out to have been the correct one; the value of
    waiting was that nothing was written on the strength of it.
    """

    index: int
    eq: tuple[DdpEqBand, ...]
    enabled: int
    polar: int
    gain_raw: int
    delay_samples: int
    eq_mode: int
    spk_type: int
    h_freq_hz: int
    h_filter: int
    h_level: int
    l_freq_hz: int
    l_filter: int
    l_level: int
    mix: tuple[int, ...]
    #: Channel-link group, 0 = unlinked. Position follows the dynamics block
    #: layout in ``docs/dsp408-protocol.md`` (threshold u8, linkgroup_num u8).
    #: Corroborated independently: outputs 7 and 8 are the only channels with a
    #: non-zero value, and they are the pair observed moving together in the
    #: vendor app. The rest of the dynamics block stays undecoded.
    linkgroup_num: int
    name: str

    @classmethod
    def decode(cls, blocks: list[bytes], index: int) -> DdpOutput:
        if len(blocks) != BLOCKS_PER_OUTPUT:
            raise ProtocolError(
                f"output record needs {BLOCKS_PER_OUTPUT} blocks, got {len(blocks)}"
            )
        misc, xover, mix = blocks[31], blocks[32], blocks[33]
        return cls(
            index=index,
            eq=tuple(DdpEqBand.decode(b) for b in blocks[:EQ_BANDS]),
            enabled=misc[0],
            polar=misc[1],
            gain_raw=_u16(misc, 2),
            delay_samples=_u16(misc, 4),
            eq_mode=misc[6],
            spk_type=misc[7],
            h_freq_hz=_u16(xover, 0),
            h_filter=xover[2],
            h_level=xover[3],
            l_freq_hz=_u16(xover, 4),
            l_filter=xover[6],
            l_level=xover[7],
            mix=tuple(mix),
            linkgroup_num=blocks[35][7],
            name=blocks[36].rstrip(b"\x00 ").decode("ascii", "replace"),
        )


@dataclass(frozen=True)
class DdpBackup:
    """A parsed vendor backup."""

    preset_name: str
    outputs: tuple[DdpOutput, ...]
    #: Every 8-byte block, verbatim, for the sections that are not decoded.
    blocks: tuple[bytes, ...]

    @property
    def global_blocks(self) -> tuple[bytes, ...]:
        return self.blocks[:INPUT_SECTION_START]

    def input_blocks(self, index: int) -> tuple[bytes, ...]:
        """Raw blocks of input record ``index``. Layout not decoded."""
        if not 0 <= index < INPUT_COUNT:
            raise IndexError(f"input {index} out of range 0..{INPUT_COUNT - 1}")
        start = INPUT_SECTION_START + index * BLOCKS_PER_INPUT
        return self.blocks[start : start + BLOCKS_PER_INPUT]


def parse(data: bytes) -> DdpBackup:
    """Parse a ``.DDP`` backup.

    Raises :class:`~tuner.dsp.protocol.ProtocolError` on anything unexpected.
    A backup that does not match the known layout must fail loudly: a
    silently mis-parsed tune produces plausible values for the wrong fields,
    which is the failure mode this whole file format is being used to avoid.
    """
    if len(data) < HEADER_LEN:
        raise ProtocolError(f"file too short: {len(data)} bytes")
    if data[0] != len(MAGIC) or data[1 : 1 + len(MAGIC)] != MAGIC:
        raise ProtocolError(f"bad magic: {data[: 1 + len(MAGIC)]!r}")

    preset_name = (
        data[1 + len(MAGIC) : HEADER_LEN].rstrip(b"\x00 ").decode("ascii", "replace")
    )
    body = data[HEADER_LEN:]
    if len(body) % BLOCK_LEN:
        raise ProtocolError(
            f"body of {len(body)} bytes is not a whole number of blocks"
        )

    blocks = [body[i : i + BLOCK_LEN] for i in range(0, len(body), BLOCK_LEN)]
    if len(blocks) != TOTAL_BLOCKS:
        raise ProtocolError(f"expected {TOTAL_BLOCKS} blocks, got {len(blocks)}")

    outputs = []
    for i in range(OUTPUT_COUNT):
        start = OUTPUT_SECTION_START + i * BLOCKS_PER_OUTPUT
        outputs.append(
            DdpOutput.decode(blocks[start : start + BLOCKS_PER_OUTPUT], i + 1)
        )

    return DdpBackup(
        preset_name=preset_name,
        outputs=tuple(outputs),
        blocks=tuple(blocks),
    )


def serialize(backup: DdpBackup) -> bytes:
    """Render a parsed backup back to file bytes.

    Exact inverse of :func:`parse` -- ``serialize(parse(data)) == data`` for any
    file this module accepts, which is what makes it safe to build a restore
    file from one.

    This exists so the project has a restore path that **shares no code with
    its own writer**. Our RFCOMM backend and the vendor Windows app are
    independent implementations; if ours has a bug that corrupts a channel, a
    file the vendor app can load is the way out. That matters more than usual
    here, because the device has no undo and there is only one unit.
    """
    if len(backup.blocks) != TOTAL_BLOCKS:
        raise ProtocolError(
            f"backup holds {len(backup.blocks)} blocks, expected {TOTAL_BLOCKS}"
        )
    for i, block in enumerate(backup.blocks):
        if len(block) != BLOCK_LEN:
            raise ProtocolError(
                f"block {i} is {len(block)} bytes, expected {BLOCK_LEN}"
            )

    name = backup.preset_name.encode("ascii", "replace")[:NAME_LEN]
    header = bytes([len(MAGIC)]) + MAGIC + name.ljust(NAME_LEN, b"\x00")
    return header + b"".join(backup.blocks)


def splice_outputs(template: DdpBackup, records: Sequence[bytes]) -> bytes:
    """A ``.DDP`` carrying ``records`` in place of the template's outputs.

    ``records`` are eight 296-byte whole-channel records, exactly as
    ``DataID 119`` returns them and as :mod:`tuner.dsp.snapshot` stores them.
    The template supplies the global, input and trailing sections, which are
    carried through **unexamined** -- we have not decoded them.

    Two caveats that belong at the call site, not buried:

    * The template must come from **the same device**, or those undecoded
      sections describe a different unit.
    * Load one of these into the vendor app once as a rehearsal before relying
      on it. An untested restore path is not a restore path.
    """
    if len(records) != OUTPUT_COUNT:
        raise ProtocolError(f"need {OUTPUT_COUNT} channel records, got {len(records)}")
    expected = BLOCKS_PER_OUTPUT * BLOCK_LEN
    for i, record in enumerate(records):
        if len(record) != expected:
            raise ProtocolError(
                f"record {i} is {len(record)} bytes, expected {expected}"
            )

    blocks = list(template.blocks)
    for i, record in enumerate(records):
        start = OUTPUT_SECTION_START + i * BLOCKS_PER_OUTPUT
        for j in range(BLOCKS_PER_OUTPUT):
            blocks[start + j] = record[j * BLOCK_LEN : (j + 1) * BLOCK_LEN]

    return serialize(
        DdpBackup(
            preset_name=template.preset_name,
            outputs=template.outputs,
            blocks=tuple(blocks),
        )
    )


def diff(before: DdpBackup, after: DdpBackup) -> list[str]:
    """Human-readable differences between two backups.

    The intended loop is: save a backup, change exactly one control in the
    vendor app, save a second backup, and read the encoding off this diff.
    Undecoded sections are compared block-by-block so that a change landing
    outside the output records is still visible rather than silently dropped.
    """
    out: list[str] = []
    #: Block indices already accounted for by a field-level line below. Any
    #: differing block *not* in here is reported raw, including blocks inside
    #: output records that this module does not decode -- the two dynamics
    #: blocks. Excluding the whole output section from the raw sweep, as an
    #: earlier version did, made those two invisible, which is exactly the
    #: silent-loss failure this tool exists to prevent.
    explained: set[int] = set()

    if before.preset_name != after.preset_name:
        out.append(f"preset name: {before.preset_name!r} -> {after.preset_name!r}")

    for b_out, a_out in zip(before.outputs, after.outputs, strict=True):
        tag = f"OUT{b_out.index}"
        base = OUTPUT_SECTION_START + (b_out.index - 1) * BLOCKS_PER_OUTPUT
        for offset, field_group in (
            (
                31,
                (
                    "enabled",
                    "polar",
                    "gain_raw",
                    "delay_samples",
                    "eq_mode",
                    "spk_type",
                ),
            ),
            (
                32,
                (
                    "h_freq_hz",
                    "h_filter",
                    "h_level",
                    "l_freq_hz",
                    "l_filter",
                    "l_level",
                ),
            ),
            (33, ("mix",)),
            (36, ("name",)),
        ):
            if any(getattr(b_out, f) != getattr(a_out, f) for f in field_group):
                explained.add(base + offset)
        for band in range(EQ_BANDS):
            if b_out.eq[band] != a_out.eq[band]:
                explained.add(base + band)

        for field in (
            "enabled",
            "polar",
            "gain_raw",
            "delay_samples",
            "eq_mode",
            "spk_type",
            "h_freq_hz",
            "h_filter",
            "h_level",
            "l_freq_hz",
            "l_filter",
            "l_level",
            "mix",
            "linkgroup_num",
            "name",
        ):
            b_val, a_val = getattr(b_out, field), getattr(a_out, field)
            if b_val != a_val:
                out.append(f"{tag}.{field}: {b_val} -> {a_val}")
        for band, (b_band, a_band) in enumerate(zip(b_out.eq, a_out.eq, strict=True)):
            if b_band != a_band:
                for field in ("freq_hz", "level_raw", "bw_raw", "shf_db", "type"):
                    b_val, a_val = getattr(b_band, field), getattr(a_band, field)
                    if b_val != a_val:
                        out.append(f"{tag}.eq[{band}].{field}: {b_val} -> {a_val}")

    for i, (b_blk, a_blk) in enumerate(zip(before.blocks, after.blocks, strict=True)):
        if b_blk == a_blk or i in explained:
            continue
        out.append(f"block[{i}] ({_where(i)}): {b_blk.hex(' ')} -> {a_blk.hex(' ')}")

    return out


def _where(block_index: int) -> str:
    """Human-readable location of an undecoded block, for diff output."""
    if block_index < INPUT_SECTION_START:
        return f"global block {block_index}"
    if block_index < OUTPUT_SECTION_START:
        rel = block_index - INPUT_SECTION_START
        return f"input {rel // BLOCKS_PER_INPUT + 1} block {rel % BLOCKS_PER_INPUT}"
    if block_index < TRAILING_SECTION_START:
        rel = block_index - OUTPUT_SECTION_START
        offset = rel % BLOCKS_PER_OUTPUT
        # 34/35 are named for what they contain rather than for what
        # protocol.OutputBlock calls them -- see the warning there. The two
        # modules disagree, and this one matches the bytes.
        name = {
            34: "dynamics A, undecoded (protocol.py calls this MIX_IN_9_16)",
            35: "dynamics B, only linkgroup_num decoded",
        }.get(offset, f"block {offset}, undecoded")
        return f"OUT{rel // BLOCKS_PER_OUTPUT + 1} {name}"
    return f"trailing block {block_index - TRAILING_SECTION_START}"
