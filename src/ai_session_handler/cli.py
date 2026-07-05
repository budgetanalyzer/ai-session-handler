"""Command-line interface for AI Session Handler."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from ai_session_handler import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser."""
    parser = argparse.ArgumentParser(
        prog="ai-session-handler",
        description="Run one provider-agnostic AI agent plan phase and stop.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the package version and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"ai-session-handler {__version__}")
        return 0

    parser.print_help()
    return 0
