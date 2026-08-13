"""Tests for the HCI snoop log reader.

Captures are built synthetically here so the reader can be exercised without a
real log, and so the awkward cases -- L2CAP fragmentation, truncated tails,
frames on an unmodelled transport -- are covered deliberately rather than by
whatever happened to be in one capture.
"""

from __future__ import annotations

import struct

import pytest

from tuner.dsp.btsnoop import (
    ATT_HANDLE_VALUE_NOTIFICATION,
    ATT_WRITE_COMMAND,
    CID_ATT,
    DSP408_DLCI,
    H4_ACL,
    H4_EVENT,
    MAGIC,
    SnoopError,
    att_pdus,
    captured_frames,
    packets,
    parse_header,
    rfcomm_streams,
)
from tuner.dsp.protocol import DataType, Frame, FrameType

WRITE_HANDLE = 0x0012


def _frame(channel: int = 0, payload: bytes = b"\x01\x02\x03\x04") -> bytes:
    return Frame(
        frame_type=FrameType.WRITE,
        data_type=DataType.OUTPUT_CHANNEL,
        channel_id=channel,
        data_id=31,
        payload=payload,
    ).encode()


def _att_write(handle: int, value: bytes) -> bytes:
    return bytes([ATT_WRITE_COMMAND]) + handle.to_bytes(2, "little") + value


def _acl(conn: int, body: bytes, pb: int = 0x2) -> bytes:
    l2cap = struct.pack("<HH", len(body), CID_ATT) + body
    header = struct.pack("<HH", (pb << 12) | conn, len(l2cap))
    return bytes([H4_ACL]) + header + l2cap


def _acl_fragments(conn: int, body: bytes, mtu: int) -> list[bytes]:
    """Split one L2CAP PDU across ACL packets, as a real stack would."""
    l2cap = struct.pack("<HH", len(body), CID_ATT) + body
    out, first = [], True
    for i in range(0, len(l2cap), mtu):
        chunk = l2cap[i : i + mtu]
        header = struct.pack("<HH", ((0x2 if first else 0x1) << 12) | conn, len(chunk))
        out.append(bytes([H4_ACL]) + header + chunk)
        first = False
    return out


def _rfcomm_acl(conn: int, dlci: int, data: bytes, *, cid: int = 0x0049) -> bytes:
    """One RFCOMM UIH frame on a dynamically allocated L2CAP channel.

    Address octet is ``EA(1) | C/R(1) | DLCI(6)``; control ``0xEF`` is UIH with
    P/F clear, so no credit octet precedes the data. The trailing byte is the
    FCS, which the reader does not check.
    """
    assert len(data) < 128, "test helper only emits the 7-bit length form"
    body = bytes([(dlci << 2) | 0x03, 0xEF, (len(data) << 1) | 1]) + data + b"\x00"
    l2cap = struct.pack("<HH", len(body), cid) + body
    header = struct.pack("<HH", (0x2 << 12) | conn, len(l2cap))
    return bytes([H4_ACL]) + header + l2cap


def _capture(entries: list[tuple[bytes, bool, int]]) -> bytes:
    out = bytearray(struct.pack(">8sII", MAGIC, 1, 1002))
    for data, received, timestamp in entries:
        out += struct.pack(
            ">IIIIq", len(data), len(data), 1 if received else 0, 0, timestamp
        )
        out += data
    return bytes(out)


class TestHeader:
    def test_rejects_bad_magic(self):
        with pytest.raises(SnoopError, match="bad magic"):
            parse_header(struct.pack(">8sII", b"nope\x00\x00\x00\x00", 1, 1002))

    def test_rejects_short_file(self):
        with pytest.raises(SnoopError, match="too short"):
            parse_header(b"btsnoop\x00")

    def test_rejects_unknown_version(self):
        with pytest.raises(SnoopError, match="version"):
            parse_header(struct.pack(">8sII", MAGIC, 9, 1002))

    def test_returns_the_datalink_type(self):
        assert parse_header(struct.pack(">8sII", MAGIC, 1, 1002)) == 1002


class TestPackets:
    def test_reads_direction_and_timestamp(self):
        data = _capture([(b"\x02abc", False, 1000), (b"\x04def", True, 2000)])
        got = packets(data)
        assert [p.received for p in got] == [False, True]
        assert [p.timestamp_us for p in got] == [1000, 2000]
        assert got[1].h4_type == H4_EVENT

    def test_tolerates_a_truncated_final_record(self):
        # Snoop logs are routinely cut off mid-write when pulled. Losing the
        # tail is expected; refusing to read the rest would be useless.
        data = bytearray(_capture([(b"\x02abc", False, 1), (b"\x02defgh", False, 2)]))
        got = packets(bytes(data[:-3]))
        assert len(got) == 1

    def test_empty_capture(self):
        assert packets(_capture([])) == []


class TestAttReassembly:
    def test_single_packet_write(self):
        data = _capture([(_acl(0x40, _att_write(WRITE_HANDLE, b"\xaa\xbb")), False, 1)])
        (pdu,) = att_pdus(packets(data))
        assert pdu.opcode == ATT_WRITE_COMMAND
        assert pdu.handle == WRITE_HANDLE
        assert pdu.value == b"\xaa\xbb"
        assert pdu.opcode_name == "write command"

    def test_fragmented_write_is_reassembled(self):
        # A whole-channel write is 312 bytes against an observed MTU of 120, so
        # this is the normal case for the largest frames, not an edge case.
        value = bytes(range(256))
        entries = [
            (frag, False, 10 + i)
            for i, frag in enumerate(
                _acl_fragments(0x40, _att_write(WRITE_HANDLE, value), mtu=64)
            )
        ]
        assert len(entries) > 1, "test needs actual fragmentation"
        (pdu,) = att_pdus(packets(_capture(entries)))
        assert pdu.value == value

    def test_notifications_are_captured_with_their_handle(self):
        body = bytes([ATT_HANDLE_VALUE_NOTIFICATION]) + (0x0015).to_bytes(2, "little")
        data = _capture([(_acl(0x40, body + b"\x51"), True, 1)])
        (pdu,) = att_pdus(packets(data))
        assert pdu.handle == 0x0015
        assert pdu.received is True

    def test_non_att_channels_are_ignored(self):
        l2cap = struct.pack("<HH", 2, 0x0005) + b"\xff\xff"
        header = struct.pack("<HH", (0x2 << 12) | 0x40, len(l2cap))
        data = _capture([(bytes([H4_ACL]) + header + l2cap, False, 1)])
        assert att_pdus(packets(data)) == []

    def test_orphan_continuation_does_not_crash(self):
        frag = _acl_fragments(0x40, _att_write(WRITE_HANDLE, bytes(200)), mtu=64)[1]
        assert att_pdus(packets(_capture([(frag, False, 1)]))) == []


class TestCapturedFrames:
    def test_finds_a_frame_and_its_handle(self):
        raw = _frame(channel=3)
        data = _capture([(_acl(0x40, _att_write(WRITE_HANDLE, raw)), False, 5_000_000)])
        (found,) = captured_frames(data)
        assert found.handle == WRITE_HANDLE
        assert found.frame.channel_id == 3
        assert found.raw == raw
        assert found.received is False

    def test_offsets_are_relative_to_the_first_packet(self):
        # Absolute btsnoop timestamps vary by stack; relative time does not,
        # and relative time is what lines frames up against a list of actions.
        a = _acl(0x40, _att_write(WRITE_HANDLE, _frame(0)))
        b = _acl(0x40, _att_write(WRITE_HANDLE, _frame(1)))
        data = _capture([(a, False, 1_000_000), (b, False, 4_500_000)])
        offsets = [f.offset_s for f in captured_frames(data)]
        assert offsets == pytest.approx([0.0, 3.5])

    def test_finds_frames_carried_on_an_unmodelled_transport(self):
        # Reported with handle=None rather than dropped: a frame we cannot
        # attribute is still evidence, and silently losing it would be the
        # worst outcome for a tool whose job is "what did the app send?".
        data = _capture([(bytes([H4_ACL]) + b"\x00" * 4 + _frame(2), False, 1)])
        (found,) = captured_frames(data)
        assert found.handle is None
        assert found.frame.channel_id == 2

    def test_several_frames_in_one_pdu(self):
        both = _frame(0) + _frame(1)
        data = _capture([(_acl(0x40, _att_write(WRITE_HANDLE, both)), False, 1)])
        assert [f.frame.channel_id for f in captured_frames(data)] == [0, 1]

    def test_ignores_a_preamble_that_is_not_a_valid_frame(self):
        # 80 80 80 EE can occur by chance inside audio-ish data. Only bytes
        # that decode with a correct checksum count.
        junk = b"\x80\x80\x80\xee" + bytes(20)
        data = _capture([(_acl(0x40, _att_write(WRITE_HANDLE, junk)), False, 1)])
        assert captured_frames(data) == []

    def test_no_frames_in_an_unrelated_capture(self):
        data = _capture([(b"\x04\x0e\x04\x01\x03\x0c\x00", True, 1)])
        assert captured_frames(data) == []


class TestRfcommStreams:
    """DLCI handling.

    RFCOMM multiplexes several data link connections over one L2CAP channel.
    Folding them all into a single byte stream concatenates unrelated traffic,
    and folding in DLCI 0 -- the multiplexer control channel -- prepends link
    management to the data. In the real capture that is exactly 18 bytes of
    PN/MSC in each direction, which is enough to shift every byte offset while
    leaving frame *recovery* working, because frames are found by preamble.
    That is the dangerous shape of bug: invisible until something compares
    offsets.
    """

    def test_control_channel_is_excluded(self):
        data = _capture(
            [
                (_rfcomm_acl(0x0B, 0, b"\xe3\x05\x0b\x8d"), False, 1),
                (_rfcomm_acl(0x0B, 2, _frame(0)), False, 2),
            ]
        )
        stream, chunks = rfcomm_streams(packets(data))[False]
        assert stream == _frame(0)
        assert [c.length for c in chunks] == [len(_frame(0))]

    def test_control_bytes_would_otherwise_shift_every_offset(self):
        # The regression stated as an offset rather than a byte count, because
        # the offset is what a replay oracle compares. The real capture's PN
        # and MSC exchange is 18 bytes each way; this is the same shape.
        pn = b"\x83\x11\x02\xf0\x00\x00\xde\x03\x00\x07"
        data = _capture(
            [
                (_rfcomm_acl(0x0B, 0, pn), False, 1),
                (_rfcomm_acl(0x0B, 2, _frame(0)), False, 2),
            ]
        )
        (chunk,) = rfcomm_streams(packets(data))[False][1]
        assert chunk.offset == 0, "the frame must start at 0, not after the PN"

    def test_a_specific_dlci_can_be_selected(self):
        data = _capture(
            [
                (_rfcomm_acl(0x0B, 2, _frame(0)), False, 1),
                (_rfcomm_acl(0x0B, 4, _frame(1)), False, 2),
            ]
        )
        pkts = packets(data)
        assert rfcomm_streams(pkts, DSP408_DLCI)[False][0] == _frame(0)
        assert rfcomm_streams(pkts, 4)[False][0] == _frame(1)
        # Unfiltered still concatenates both data channels.
        assert rfcomm_streams(pkts)[False][0] == _frame(0) + _frame(1)

    def test_directions_are_kept_apart(self):
        data = _capture(
            [
                (_rfcomm_acl(0x0B, 2, _frame(0)), False, 1),
                (_rfcomm_acl(0x0B, 2, _frame(1)), True, 2),
            ]
        )
        streams = rfcomm_streams(packets(data), DSP408_DLCI)
        assert streams[False][0] == _frame(0)
        assert streams[True][0] == _frame(1)
