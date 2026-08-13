"""Reader for Android's Bluetooth HCI snoop log.

Purpose: turn a passive capture of the vendor app into three things we cannot
get any other way without writing to the only DSP we have.

1. **Which GATT handle the app writes to.** ``0xFFE2`` is currently inferred
   from the characteristic properties, not observed.
2. **Known-good frames** to validate :mod:`tuner.dsp.protocol`'s encoder
   against before it ever transmits.
3. **Whether one UI change is one frame.** The OUT7/8 incident raised the
   question of write scope and could not settle it.

Pure logic, no I/O -- it takes bytes. ``tools/btsnoop_extract.py`` is the CLI.

Format, from the btsnoop specification::

    header:  8s  "btsnoop\\0"
             u32 version (big-endian, 1)
             u32 datalink type (big-endian)
    record:  u32 original length
             u32 included length
             u32 packet flags
             u32 cumulative drops
             i64 timestamp, microseconds
             ... included length bytes of packet

Everything in the header and record fields is **big-endian**, while everything
inside the HCI payload is little-endian. Getting that backwards produces
plausible-looking garbage rather than an error.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .protocol import HEADER_LEN, OVERHEAD, VENDOR_HEAD, Frame, ProtocolError, decode

MAGIC = b"btsnoop\x00"
_HEADER = struct.Struct(">8sII")
_RECORD = struct.Struct(">IIIIq")

#: btsnoop timestamps count microseconds from 0000-01-01 in the proleptic
#: Gregorian calendar. 719_528 days separate that from the Unix epoch.
#:
#: Some stacks use a slightly different origin, so an absolute timestamp from
#: this module is best-effort. **Relative time within a capture is exact**, and
#: that is what matters for lining frames up against a list of UI actions.
_EPOCH_DELTA_US = 719_528 * 86_400 * 1_000_000

#: H4 packet type indicators, the first byte of each HCI packet.
H4_COMMAND = 0x01
H4_ACL = 0x02
H4_EVENT = 0x04

#: L2CAP channel identifier for the attribute protocol.
CID_ATT = 0x0004

#: L2CAP fixed channels. Anything else is a dynamically allocated classic
#: channel, which is how RFCOMM (and therefore SPP) is carried.
CID_FIXED = frozenset({0x0001, 0x0002, 0x0003, 0x0004, 0x0005, 0x0006, 0x0007})

#: RFCOMM's multiplexer control channel. Carries PN, MSC and other link
#: management, never user data, so it is excluded from the data streams.
RFCOMM_CONTROL_DLCI = 0

#: The DLCI the DSP-408's vendor app uses -- SPP server channel 1. Measured
#: from captures/btsnoop_hci.log, not from an SDP record.
DSP408_DLCI = 2

#: ATT opcodes that carry a handle followed by a value.
ATT_WRITE_REQUEST = 0x12
ATT_WRITE_COMMAND = 0x52
ATT_HANDLE_VALUE_NOTIFICATION = 0x1B
ATT_HANDLE_VALUE_INDICATION = 0x1D
ATT_READ_RESPONSE = 0x0B

_ATT_WITH_HANDLE = {
    ATT_WRITE_REQUEST: "write request",
    ATT_WRITE_COMMAND: "write command",
    ATT_HANDLE_VALUE_NOTIFICATION: "notification",
    ATT_HANDLE_VALUE_INDICATION: "indication",
}


class SnoopError(ValueError):
    """Raised when bytes do not form a readable btsnoop capture."""


@dataclass(frozen=True)
class Packet:
    """One HCI packet from the capture."""

    index: int
    timestamp_us: int
    #: True when the packet travelled controller -> host.
    received: bool
    data: bytes

    @property
    def h4_type(self) -> int | None:
        return self.data[0] if self.data else None

    def utc(self) -> datetime:
        """Best-effort wall-clock time. See ``_EPOCH_DELTA_US``."""
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            microseconds=self.timestamp_us - _EPOCH_DELTA_US
        )


@dataclass(frozen=True)
class AttPdu:
    """One reassembled ATT protocol data unit."""

    packet_index: int
    timestamp_us: int
    received: bool
    opcode: int
    handle: int | None
    value: bytes

    @property
    def opcode_name(self) -> str:
        return _ATT_WITH_HANDLE.get(self.opcode, f"opcode 0x{self.opcode:02X}")


@dataclass(frozen=True)
class CapturedFrame:
    """A DSP-408 protocol frame recovered from the capture."""

    packet_index: int
    timestamp_us: int
    #: Seconds since the first packet in the capture. Exact, unlike ``utc``.
    offset_s: float
    received: bool
    #: How the frame was recovered: "rfcomm" (classic SPP, what the vendor
    #: app uses), "att" (BLE), or "raw" (backstop byte scan).
    transport: str
    #: GATT attribute handle, only meaningful for ``transport == "att"``.
    handle: int | None
    att_opcode: int | None
    raw: bytes
    frame: Frame


def parse_header(data: bytes) -> int:
    """Validate the file header and return the datalink type."""
    if len(data) < _HEADER.size:
        raise SnoopError(f"file is {len(data)} bytes, too short for a header")
    magic, version, datalink = _HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise SnoopError(f"bad magic {magic!r}, expected {MAGIC!r}")
    if version != 1:
        raise SnoopError(f"unsupported btsnoop version {version}")
    return datalink


def packets(data: bytes) -> list[Packet]:
    """Split a capture into packets.

    A truncated final record is tolerated -- snoop logs are frequently cut off
    mid-write when they are pulled -- but anything else malformed raises.
    """
    parse_header(data)
    out: list[Packet] = []
    offset = _HEADER.size
    while offset + _RECORD.size <= len(data):
        _, included, flags, _, timestamp = _RECORD.unpack_from(data, offset)
        offset += _RECORD.size
        if offset + included > len(data):
            break  # truncated tail
        out.append(
            Packet(
                index=len(out),
                timestamp_us=timestamp,
                received=bool(flags & 0x1),
                data=data[offset : offset + included],
            )
        )
        offset += included
    return out


def att_pdus(pkts: list[Packet]) -> list[AttPdu]:
    """Reassemble ATT PDUs from H4 ACL packets.

    L2CAP fragments across ACL packets once a payload exceeds the negotiated
    MTU, and a whole-channel write to this device is 312 bytes against an
    observed MTU of 120 -- so reassembly is required, not optional. Dropping
    continuation fragments would silently lose exactly the largest and most
    interesting frames.
    """
    out: list[AttPdu] = []
    pending: dict[int, dict] = {}

    for pkt in pkts:
        if pkt.h4_type != H4_ACL or len(pkt.data) < 5:
            continue
        handle_flags, acl_len = struct.unpack_from("<HH", pkt.data, 1)
        conn = handle_flags & 0x0FFF
        pb = (handle_flags >> 12) & 0x3
        payload = pkt.data[5 : 5 + acl_len]

        if pb == 0x1:  # continuation of an earlier L2CAP PDU
            state = pending.get(conn)
            if state is None:
                continue
            state["buf"] += payload
        else:  # start of a new L2CAP PDU
            if len(payload) < 4:
                continue
            l2cap_len, cid = struct.unpack_from("<HH", payload, 0)
            pending[conn] = {
                "buf": payload[4:],
                "want": l2cap_len,
                "cid": cid,
                "packet": pkt,
            }
            state = pending[conn]

        if len(state["buf"]) < state["want"]:
            continue

        body = state["buf"][: state["want"]]
        cid, first = state["cid"], state["packet"]
        pending.pop(conn, None)
        if cid != CID_ATT or not body:
            continue

        opcode = body[0]
        if opcode in _ATT_WITH_HANDLE and len(body) >= 3:
            handle = int.from_bytes(body[1:3], "little")
            value = body[3:]
        else:
            handle, value = None, body[1:]
        out.append(
            AttPdu(
                packet_index=first.index,
                timestamp_us=first.timestamp_us,
                received=first.received,
                opcode=opcode,
                handle=handle,
                value=value,
            )
        )
    return out


@dataclass(frozen=True)
class StreamChunk:
    """A run of RFCOMM payload bytes and where it landed in the stream."""

    offset: int
    timestamp_us: int
    length: int


def rfcomm_streams(
    pkts: list[Packet],
    dlci: int | None = None,
) -> dict[bool, tuple[bytes, list[StreamChunk]]]:
    """Reassemble the RFCOMM byte streams, keyed by direction.

    **RFCOMM is a byte stream, not a message channel.** A protocol frame is not
    guaranteed to sit inside one RFCOMM frame and in practice does not: the
    observed payload is 20 bytes, while any parameter write is at least 24, so
    **every write is split across two RFCOMM frames.** Scanning packets
    individually finds the short read frames and silently misses every write --
    which is exactly what it did before this function existed.

    **DLCI 0 is always excluded.** It is RFCOMM's multiplexer control channel
    and by definition never carries user data -- it carries PN, MSC and the
    like. Folding it into the data stream prepends 18 bytes of control traffic
    to both directions, which shifts every subsequent byte offset by 18. That
    is harmless when the caller only wants frames located by preamble, and
    quietly wrong for anything that compares our offsets against the real
    stream. Pass ``dlci`` to select one data channel; the DSP-408's vendor app
    uses DLCI 2, i.e. SPP server channel 1.

    Returns ``{received: (stream_bytes, chunks)}``. The chunks let a byte
    offset be mapped back to the packet timestamp it arrived in.
    """
    streams: dict[bool, bytearray] = {False: bytearray(), True: bytearray()}
    chunks: dict[bool, list[StreamChunk]] = {False: [], True: []}
    pending: dict[int, dict] = {}

    for pkt in pkts:
        if pkt.h4_type != H4_ACL or len(pkt.data) < 9:
            continue
        handle_flags, acl_len = struct.unpack_from("<HH", pkt.data, 1)
        conn = handle_flags & 0x0FFF
        pb = (handle_flags >> 12) & 0x3
        body = pkt.data[5 : 5 + acl_len]

        if pb == 0x1:
            state = pending.get(conn)
            if state is None:
                continue
            state["buf"] += body
        else:
            if len(body) < 4:
                continue
            l2cap_len, cid = struct.unpack_from("<HH", body, 0)
            pending[conn] = state = {
                "buf": bytearray(body[4:]),
                "want": l2cap_len,
                "cid": cid,
                "packet": pkt,
            }

        if len(state["buf"]) < state["want"]:
            continue
        payload = bytes(state["buf"][: state["want"]])
        cid, first = state["cid"], state["packet"]
        pending.pop(conn, None)

        if cid in CID_FIXED or len(payload) < 4:
            continue
        # Address octet: EA(1) | C/R(1) | DLCI(6).
        frame_dlci = payload[0] >> 2
        if frame_dlci == RFCOMM_CONTROL_DLCI:
            continue
        if dlci is not None and frame_dlci != dlci:
            continue
        control = payload[1]
        if control & 0xEF != 0xEF:  # only UIH frames carry data
            continue
        length_byte = payload[2]
        if length_byte & 1:  # 7-bit length
            data_len, offset = length_byte >> 1, 3
        else:  # 15-bit length spanning two octets
            data_len, offset = (length_byte >> 1) | (payload[3] << 7), 4
        if control & 0x10:  # P/F set means a credit octet precedes the data
            offset += 1

        data = payload[offset : offset + data_len]
        if not data:
            continue
        stream = streams[first.received]
        chunks[first.received].append(
            StreamChunk(len(stream), first.timestamp_us, len(data))
        )
        stream += data

    return {k: (bytes(v), chunks[k]) for k, v in streams.items()}


def _scan(buf: bytes) -> list[tuple[int, bytes]]:
    """Every well-formed DSP-408 frame in a buffer, with its offset.

    Located by preamble, length and checksum rather than by position, so a
    frame survives whatever transport wrapping sits around it -- and a chance
    occurrence of the preamble in unrelated data is rejected rather than
    reported.
    """
    found: list[tuple[int, bytes]] = []
    start = buf.find(VENDOR_HEAD)
    while start != -1:
        if start + HEADER_LEN <= len(buf):
            n = buf[start + 12] | (buf[start + 13] << 8)
            end = start + n + OVERHEAD
            if end <= len(buf):
                candidate = buf[start:end]
                try:
                    decode(candidate)
                except ProtocolError:
                    pass
                else:
                    found.append((start, candidate))
                    start = buf.find(VENDOR_HEAD, end)
                    continue
        start = buf.find(VENDOR_HEAD, start + 1)
    return found


def _timestamp_at(chunks: list[StreamChunk], offset: int) -> int:
    """Timestamp of the packet that delivered the byte at ``offset``."""
    import bisect

    starts = [c.offset for c in chunks]
    i = max(bisect.bisect_right(starts, offset) - 1, 0)
    return chunks[i].timestamp_us if chunks else 0


def captured_frames(data: bytes) -> list[CapturedFrame]:
    """Every DSP-408 frame in a capture.

    Three transports are searched, because the vendor tooling does not use the
    one this project assumed:

    1. **RFCOMM byte streams** (classic Bluetooth SPP) -- what the Android app
       actually uses. Frames are found in the reassembled stream, so writes
       split across RFCOMM frames are recovered.
    2. **ATT PDUs** (BLE), reassembled across L2CAP fragments, which yields a
       GATT handle.
    3. **Raw packet bytes**, as a backstop, so a frame carried some other way
       is reported rather than silently dropped.

    Each frame is attributed once, by the first method that finds it.
    """
    pkts = packets(data)
    if not pkts:
        return []
    origin = pkts[0].timestamp_us
    out: list[CapturedFrame] = []
    seen: set[bytes] = set()

    for received, (stream, chunks) in rfcomm_streams(pkts).items():
        for offset, raw in _scan(stream):
            timestamp = _timestamp_at(chunks, offset)
            out.append(
                CapturedFrame(
                    packet_index=-1,
                    timestamp_us=timestamp,
                    offset_s=(timestamp - origin) / 1e6,
                    received=received,
                    transport="rfcomm",
                    handle=None,
                    att_opcode=None,
                    raw=raw,
                    frame=decode(raw),
                )
            )
            seen.add(raw)

    for pdu in att_pdus(pkts):
        for _, raw in _scan(pdu.value):
            if raw in seen:
                continue
            out.append(
                CapturedFrame(
                    packet_index=pdu.packet_index,
                    timestamp_us=pdu.timestamp_us,
                    offset_s=(pdu.timestamp_us - origin) / 1e6,
                    received=pdu.received,
                    transport="att",
                    handle=pdu.handle,
                    att_opcode=pdu.opcode,
                    raw=raw,
                    frame=decode(raw),
                )
            )
            seen.add(raw)

    for pkt in pkts:
        for _, raw in _scan(pkt.data):
            if raw in seen:
                continue
            out.append(
                CapturedFrame(
                    packet_index=pkt.index,
                    timestamp_us=pkt.timestamp_us,
                    offset_s=(pkt.timestamp_us - origin) / 1e6,
                    received=pkt.received,
                    transport="raw",
                    handle=None,
                    att_opcode=None,
                    raw=raw,
                    frame=decode(raw),
                )
            )
            seen.add(raw)

    out.sort(key=lambda f: f.timestamp_us)
    return out
