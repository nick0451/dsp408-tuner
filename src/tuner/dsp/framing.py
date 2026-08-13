"""Turning an RFCOMM byte stream into frames, and frames back into chunks.

Pure logic, no I/O, so the whole thing is testable against
``captures/btsnoop_hci.log`` with no hardware. :mod:`tuner.dsp.transport` moves
bytes; this module decides where frames begin and end.

Two facts from the capture drive every design decision here, and both were
measured rather than assumed (see ``docs/dsp408-protocol.md``):

**RFCOMM is a byte stream.** A protocol frame is not guaranteed to arrive in one
piece and routinely does not -- a 24-byte write crosses two link frames. A
reader that parses packets individually finds the short reads and silently
misses every write, which is exactly what this project's first reader did.

**Everything moves in 20-byte zero-padded chunks.** All 2939 host link frames in
the capture are exactly 20 bytes, every protocol frame starts on a 20-byte
boundary in both directions, and all 2917 inter-frame gaps are ``0x00``. So the
reader must tolerate runs of zeros between frames, and the writer pads to a
multiple of 20.

Whether the device *requires* the chunking or merely tolerates it is unknown --
only one side of that experiment is visible. The likely story is that the vendor
app shares a send routine with its BLE path, sized for BLE's 20-byte default ATT
payload. We replicate it because replication is free and tolerance is
unconfirmed; ``pad_to=None`` disables it for the day someone tests the
alternative, which must not be the only unit.
"""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import (
    FRAME_START,
    HEADER_LEN,
    OVERHEAD,
    VENDOR_HEAD,
    Frame,
    ProtocolError,
    decode,
)

#: The four bytes that open every frame.
#:
#: Searched for as four bytes rather than as :data:`VENDOR_HEAD`'s three. A run
#: of four ``0x80`` matches the three-byte head at two different offsets, and
#: taking the first would put the parse one byte early on data that is otherwise
#: perfectly good.
PREAMBLE = VENDOR_HEAD + bytes([FRAME_START])

#: Link chunk size. Measured: 2939 of 2939 host link frames.
CHUNK = 20

#: Largest payload the reader will wait for, from the vendor's own bulk buffer
#: size (``U0DataLen = 800``). The largest payload ever observed is 296, the
#: whole-channel readback.
#:
#: This bound is load-bearing, not defensive. The length field is two bytes read
#: straight off the wire, so a corrupted one can claim 65535 payload bytes; the
#: reader would then wait forever for data that is never coming, and the
#: symptom would be indistinguishable from a dead link.
MAX_PAYLOAD = 800


@dataclass
class FrameReaderStats:
    """Counters for what the stream contained besides frames.

    Worth surfacing rather than discarding. A healthy link produces frames and
    pad bytes and nothing else -- across the whole reference capture, ``resyncs``
    and ``checksum_errors`` are both zero. Anything non-zero in the others is
    the first evidence that the transport assumptions are wrong.
    """

    frames: int = 0
    pad_bytes: int = 0
    garbage_bytes: int = 0
    resyncs: int = 0
    checksum_errors: int = 0
    oversize_length: int = 0

    @property
    def clean(self) -> bool:
        """Whether the stream held only frames and padding."""
        return not (
            self.garbage_bytes
            or self.resyncs
            or self.checksum_errors
            or self.oversize_length
        )


@dataclass(frozen=True)
class ReceivedFrame:
    """A decoded frame and the raw bytes it came from.

    The raw bytes are kept because a retry must resend byte-identical data, and
    because re-encoding to compare against them is the check that catches a
    codec disagreement.
    """

    frame: Frame
    raw: bytes


class FrameReader:
    """Incremental frame extractor for a byte stream.

    Feed it whatever arrives, in whatever sizes it arrives in; it returns the
    frames that are now complete and keeps any partial tail for next time.
    ``feed`` is the only entry point and it never raises -- a stream that
    contains garbage is a fact to be counted, not an exception, because the
    caller cannot do anything about it mid-session and the frames on either side
    are still wanted.

    Not thread-safe. One reader per stream, owned by the transaction layer.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.stats = FrameReaderStats()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FrameReader(pending={len(self._buf)}, {self.stats})"

    @property
    def pending(self) -> int:
        """Bytes buffered but not yet part of a complete frame."""
        return len(self._buf)

    def reset(self) -> None:
        """Discard buffered bytes. Use on reconnect, never mid-session."""
        self._buf.clear()

    def feed(self, data: bytes) -> list[ReceivedFrame]:
        """Absorb bytes and return every frame that is now complete."""
        self._buf += data
        out: list[ReceivedFrame] = []
        while self._step(out):
            pass
        return out

    def _step(self, out: list[ReceivedFrame]) -> bool:
        """One parse attempt. Returns whether progress was made."""
        buf = self._buf

        # Inter-frame padding. Only ever stripped at the head, so a 0x00 inside
        # a payload is untouched -- by the time we are here the previous frame
        # has been consumed whole.
        zeros = 0
        while zeros < len(buf) and buf[zeros] == 0x00:
            zeros += 1
        if zeros:
            del buf[:zeros]
            self.stats.pad_bytes += zeros
            return bool(buf)

        if not buf:
            return False

        start = buf.find(PREAMBLE)
        if start == -1:
            # Keep only what could still be the head of a split preamble.
            keep = len(PREAMBLE) - 1
            if len(buf) > keep:
                self.stats.garbage_bytes += len(buf) - keep
                self.stats.resyncs += 1
                del buf[: len(buf) - keep]
            return False
        if start > 0:
            self.stats.garbage_bytes += start
            self.stats.resyncs += 1
            del buf[:start]
            return True

        if len(buf) < HEADER_LEN:
            return False

        n = buf[12] | (buf[13] << 8)
        if n > MAX_PAYLOAD:
            # Not a real frame: a chance preamble, or a corrupted length. Drop
            # one byte and look again rather than trusting the length enough to
            # skip past it.
            self.stats.oversize_length += 1
            return self._resync_one_byte()

        total = n + OVERHEAD
        if len(buf) < total:
            return False

        candidate = bytes(buf[:total])
        try:
            frame = decode(candidate)
        except ProtocolError:
            # Could be a chance preamble, or a corrupted length field pointing
            # at the wrong end byte. Advancing by `total` would skip real data
            # in the second case, so advance by one.
            self.stats.checksum_errors += 1
            return self._resync_one_byte()

        del buf[:total]
        self.stats.frames += 1
        out.append(ReceivedFrame(frame=frame, raw=candidate))
        return True

    def _resync_one_byte(self) -> bool:
        self.stats.garbage_bytes += 1
        self.stats.resyncs += 1
        del self._buf[:1]
        return bool(self._buf)


def chunk(frame: bytes, pad_to: int | None = CHUNK) -> list[bytes]:
    """Split an encoded frame into link-sized writes, as the vendor app does.

    With ``pad_to`` set, the frame is zero-padded up to a multiple of that size
    and returned as equal chunks -- a 16-byte read becomes one chunk with 4 pad
    bytes, a 24-byte write becomes two chunks with 16. With ``pad_to=None`` the
    frame is returned whole and unpadded, which is the untested alternative.

    Raises:
        ValueError: If ``frame`` is empty, or ``pad_to`` is not positive.
    """
    if not frame:
        raise ValueError("nothing to send")
    if pad_to is None:
        return [bytes(frame)]
    if pad_to <= 0:
        raise ValueError(f"pad_to must be positive, got {pad_to}")

    padded = bytes(frame) + bytes(-len(frame) % pad_to)
    return [padded[i : i + pad_to] for i in range(0, len(padded), pad_to)]


def chunk_20(frame: bytes) -> list[bytes]:
    """:func:`chunk` at the measured 20-byte link size."""
    return chunk(frame, CHUNK)
