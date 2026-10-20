"""Codex high-reasoning exec wrapper with terminal marker filtering."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from queue import Empty, Queue
from typing import Final, TextIO, cast

from ai_session_handler.markers import (
    MarkerParseError,
    TerminalMarkerFilter,
    parse_terminal_marker,
)
from ai_session_handler.provider_wrappers._common import (
    DiagnosticRenderer,
    close_process_pipes,
    join_threads,
    managed_termination_signals,
    terminate_child,
    write_stdin,
)

_DIAGNOSTIC_PREFIX: Final[str] = "[codex] "


class _DiagnosticRenderer(DiagnosticRenderer):
    def __init__(self, target: TextIO) -> None:
        super().__init__(target, _DIAGNOSTIC_PREFIX)


_write_stdin = write_stdin
_terminate_child = terminate_child
_close_process_pipes = close_process_pipes
_join_threads = join_threads
_managed_termination_signals = managed_termination_signals


def _stream_filtered_output(
    source: TextIO,
    target: TextIO,
    failures: Queue[BaseException],
) -> None:
    try:
        marker_filter = TerminalMarkerFilter()
        renderer = _DiagnosticRenderer(target)
        while True:
            chunk = source.readline()
            if chunk == "":
                break
            renderer.write(marker_filter.filter(chunk))
        renderer.write(marker_filter.finish())
        renderer.finish()
    except BaseException as error:
        failures.put(error)


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
        process: subprocess.Popen[str] | None = None
        threads: list[threading.Thread] = []
        with _managed_termination_signals() as termination_signals:
            try:
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
                failures: Queue[BaseException] = Queue()
                candidate_threads = [
                    threading.Thread(
                        target=_write_stdin,
                        args=(process.stdin, prompt),
                        daemon=True,
                    ),
                    threading.Thread(
                        target=_stream_filtered_output,
                        args=(process.stdout, sys.stdout, failures),
                        daemon=True,
                    ),
                    threading.Thread(
                        target=_stream_filtered_output,
                        args=(process.stderr, sys.stderr, failures),
                        daemon=True,
                    ),
                ]
                for thread in candidate_threads:
                    thread.start()
                    threads.append(thread)

                while process.poll() is None:
                    termination_signals.raise_if_pending()
                    try:
                        failure = failures.get(timeout=0.05)
                    except Empty:
                        continue
                    raise OSError(f"failed streaming Codex output: {failure}") from failure

                return_code = process.wait()
                _join_threads(threads)
                try:
                    failure = failures.get_nowait()
                except Empty:
                    pass
                else:
                    raise OSError(f"failed streaming Codex output: {failure}") from failure

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
                    renderer = _DiagnosticRenderer(sys.stdout)
                    renderer.write(final_message)
                    renderer.finish()

                return return_code
            finally:
                if process is not None:
                    _terminate_child(process)
                    _close_process_pipes(process)
                _join_threads(threads)


if __name__ == "__main__":
    raise SystemExit(main())
