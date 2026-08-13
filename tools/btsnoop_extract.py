"""Pull DSP-408 protocol frames out of an Android HCI snoop log.

    python tools/btsnoop_extract.py btsnoop_hci.log
    python tools/btsnoop_extract.py btsnoop_hci.log --expect 470 1234 689

``--expect`` takes decimal values and highlights any frame whose payload
contains them as 16-bit little-endian -- which is how you confirm a capture
against a written-down list of UI actions. Because the parameter scalings are
measured, a known action predicts known bytes: setting a channel gain to
-13.0 dB stores 470, so ``D6 01`` should appear in the frame that action
produced.

Nothing here writes to a device or needs one present.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tuner.dsp.btsnoop import MAGIC, captured_frames, packets  # noqa: E402
from tuner.dsp.protocol import DataType, FrameType, OutputBlock  # noqa: E402


def read_capture(path: Path) -> bytes:
    """Raw btsnoop bytes, from either a plain log or an Android bug report.

    On Android 9+ the log lives under a path that is not readable without
    root, so the normal way to retrieve it is ``adb bugreport`` -- which hands
    you a zip. Accepting the zip directly removes a manual extraction step
    whose correct path varies by vendor.
    """
    if path.suffix.lower() != ".zip":
        return path.read_bytes()

    import zipfile

    with zipfile.ZipFile(path) as archive:
        names = [
            n
            for n in archive.namelist()
            if "btsnoop" in n.lower() and not n.endswith("/")
        ]
        if not names:
            raise SystemExit(
                f"{path.name} contains no btsnoop log. Check the snoop setting was "
                f"enabled and Bluetooth restarted before the capture."
            )
        # Prefer the live log over the rotated .last, and the largest of any
        # remaining candidates -- a stack restart can leave several behind.
        names.sort(key=lambda n: (n.endswith(".last"), -archive.getinfo(n).file_size))
        for name in names:
            data = archive.read(name)
            if data.startswith(MAGIC):
                print(f"reading {name} from {path.name}")
                return data
        raise SystemExit(
            f"found {len(names)} btsnoop-ish entries in {path.name}, none with a "
            f"valid header: {names}"
        )


def _data_type(value: int) -> str:
    try:
        return DataType(value).name
    except ValueError:
        return f"type {value}"


def _data_id(data_type: int, value: int) -> str:
    if data_type != DataType.OUTPUT_CHANNEL:
        return f"id {value}"
    try:
        return OutputBlock(value).name
    except ValueError:
        return f"EQ band {value}" if value < 31 else f"id {value}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("capture", type=Path)
    ap.add_argument(
        "--expect",
        type=int,
        nargs="*",
        default=[],
        metavar="N",
        help="decimal values to flag where they appear as u16 little-endian",
    )
    ap.add_argument("--payloads", action="store_true", help="show payload hex")
    ap.add_argument(
        "--writes",
        action="store_true",
        help="show only parameter writes. The app polls the device constantly, "
        "so an unfiltered dump is thousands of reads around a handful of "
        "writes.",
    )
    args = ap.parse_args()

    data = read_capture(args.capture)
    pkts = packets(data)
    frames = captured_frames(data)

    print(f"{args.capture.name}: {len(pkts)} HCI packets, {len(frames)} DSP-408 frames")
    if not frames:
        print("\nNo frames found. Either the capture missed the session, or the")
        print("app reached the device by a transport this reader does not model.")
        return 1

    wanted = {v: bytes([v & 0xFF, (v >> 8) & 0xFF]) for v in args.expect}

    print()
    header = (
        f"{'t+s':>8}  {'dir':<4} {'handle':>6}  {'type':<14} {'block':<14} len  notes"
    )
    shown = [
        f
        for f in frames
        if not args.writes
        or (f.frame.frame_type == FrameType.WRITE and f.frame.payload)
    ]
    if args.writes:
        print(f"showing {len(shown)} parameter writes of {len(frames)} frames\n")
    print(header)
    for f in shown:
        direction = "dev>" if f.received else ">dev"
        handle = f"0x{f.handle:04X}" if f.handle is not None else f.transport
        notes = []
        if f.frame.frame_type == FrameType.READ:
            notes.append("READ")
        if f.frame.destructive:
            notes.append(f"** {f.frame.destructive} **")
        for value, pattern in wanted.items():
            if pattern in f.frame.payload:
                notes.append(f"contains {value}")
        print(
            f"{f.offset_s:8.2f}  {direction:<4} {handle:>6}  "
            f"{_data_type(f.frame.data_type):<14} "
            f"{_data_id(f.frame.data_type, f.frame.data_id):<14} "
            f"{len(f.frame.payload):>3}  {' '.join(notes)}"
        )
        if args.payloads and f.frame.payload:
            print(f"{'':>10}payload {f.frame.payload.hex(' ')}")

    print()
    transports = Counter((f.transport, f.handle) for f in frames if not f.received)
    if transports:
        print("Host -> device traffic travelled over:")
        for (transport, handle), count in transports.most_common():
            where = f" handle 0x{handle:04X}" if handle is not None else ""
            print(f"  {transport}{where}: {count} frame(s)")

    writes = [
        f
        for f in frames
        if not f.received and f.frame.frame_type == FrameType.WRITE and f.frame.payload
    ]
    if len(writes) > 1:
        gaps = [
            b.offset_s - a.offset_s for a, b in zip(writes, writes[1:], strict=False)
        ]
        bursts = 1 + sum(1 for g in gaps if g > 1.0)
        print()
        print(f"{len(writes)} parameter writes in {bursts} burst(s) >1 s apart.")
        print("  More writes than deliberate changes means the app sends every")
        print("  intermediate slider position, not just the value you settle on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
