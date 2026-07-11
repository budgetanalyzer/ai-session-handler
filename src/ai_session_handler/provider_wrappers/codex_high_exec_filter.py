"""Codex high-reasoning exec wrapper with terminal marker filtering."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import TextIO, cast

from ai_session_handler.markers import (
    MarkerKind,
    MarkerParseError,
    TerminalMarkerFilter,
    parse_terminal_marker,
)


def _sanitize_markers(text: str) -> str:
    sanitized = text
    for marker_kind in MarkerKind:
        tag = marker_kind.value
        sanitized = sanitized.replace(f"<{tag}>", f"[{tag}]")
        sanitized = sanitized.replace(f"</{tag}>", f"[/{tag}]")
    return sanitized


def _write_stdin(stream: TextIO, prompt: str) -> None:
    try:
        stream.write(prompt)
    except (BrokenPipeError, OSError, ValueError):
        return
    finally:
        with suppress(BrokenPipeError, OSError, ValueError):
            stream.close()


def _stream_filtered_output(source: TextIO, target: TextIO) -> None:
    marker_filter = TerminalMarkerFilter()
    while True:
        chunk = source.readline()
        if chunk == "":
            break
        visible_chunk = _sanitize_markers(marker_filter.filter(chunk))
        if visible_chunk:
            target.write(visible_chunk)
            target.flush()


def _parse_model(argv: Sequence[str] | None) -> str | None:
    parser = argparse.ArgumentParser(
        prog="ai-session-handler-codex-high",
        description="Run codex-lean exec with high reasoning and terminal marker filtering.",
    )
    parser.add_argument("--model", help="Codex CLI model slug to pass through as CODEX_MODEL")
    args = parser.parse_args(argv)
    return cast(str | None, args.model)


def main(argv: Sequence[str] | None = None) -> int:
    """Run Codex in high-reasoning mode and emit only one terminal marker."""
    model = _parse_model(argv)
    prompt = sys.stdin.read()
    env = os.environ.copy()
    env["CODEX_REASONING_EFFORT"] = "high"
    if model is not None:
        env["CODEX_MODEL"] = model

    with tempfile.TemporaryDirectory(prefix="ai-session-handler-codex-") as temp_dir:
        final_message_path = Path(temp_dir) / "final-message.txt"
        process = subprocess.Popen(
            [
                "codex-lean",
                "exec",
                "--color",
                "never",
                "--output-last-message",
                str(final_message_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            encoding="utf-8",
            errors="replace",
        )

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        threads = [
            threading.Thread(target=_write_stdin, args=(process.stdin, prompt), daemon=True),
            threading.Thread(
                target=_stream_filtered_output,
                args=(process.stdout, sys.stdout),
                daemon=True,
            ),
            threading.Thread(
                target=_stream_filtered_output,
                args=(process.stderr, sys.stderr),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()

        return_code = process.wait()
        for thread in threads:
            thread.join(timeout=1)

        try:
            final_message = final_message_path.read_text(encoding="utf-8")
        except OSError:
            final_message = ""

        try:
            marker = parse_terminal_marker(final_message)
        except MarkerParseError:
            marker = None

        if marker is not None:
            tag = marker.kind.value
            sys.stdout.write(f"<{tag}>{marker.text}</{tag}>\n")
        elif final_message.strip():
            sys.stdout.write(_sanitize_markers(final_message))
            if not final_message.endswith("\n"):
                sys.stdout.write("\n")

        return return_code


if __name__ == "__main__":
    raise SystemExit(main())
