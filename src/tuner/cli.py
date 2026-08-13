"""Command-line entry point.

Subcommands are added as milestones land. The DSP backend defaults to the
simulator; hardware is opt-in via an explicit flag, so no command can touch a
real DSP by accident.

Status: skeleton. Subcommands arrive with Milestone 1.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tuner",
        description="Automated acoustic tuning for the Dayton Audio DSP-408.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--backend",
        default="sim",
        choices=["sim", "dsp408-usb", "dsp408-i2c"],
        help="DSP backend; defaults to the simulator so no command can reach "
        "real hardware unintentionally.",
    )

    sub = parser.add_subparsers(dest="command", required=False)
    sub.add_parser("devices", help="list audio devices and their capabilities")
    sub.add_parser("measure", help="run a measurement sweep")
    sub.add_parser("calibrate", help="derive a microphone calibration curve")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command is None:
        build_parser().print_help()
        return 0

    print(f"'{args.command}' is not implemented yet; see CLAUDE.md milestones.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
