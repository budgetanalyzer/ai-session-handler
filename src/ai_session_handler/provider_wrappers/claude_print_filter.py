"""Claude effort-level print wrappers with stream-json marker filtering."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from json import JSONDecodeError
from queue import Empty, Queue
from typing import Final, Literal, TextIO, cast

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

_DIAGNOSTIC_PREFIX: Final[str] = "[claude] "
_SUMMARY_LIMIT: Final[int] = 160

type _Effort = Literal["high", "medium"]

_write_stdin = write_stdin
_terminate_child = terminate_child
_close_process_pipes = close_process_pipes
_join_threads = join_threads
_managed_termination_signals = managed_termination_signals


class _DiagnosticRenderer(DiagnosticRenderer):
    def __init__(self, target: TextIO) -> None:
        super().__init__(target, _DIAGNOSTIC_PREFIX)


@dataclass(slots=True)
class _FinalResult:
    seen: bool = False
    text: str | None = None
    is_error: bool = False


def _parse_model(effort: _Effort, argv: Sequence[str] | None) -> str | None:
    parser = argparse.ArgumentParser(
        prog=f"ai-session-handler-claude-{effort}",
        description=(
            f"Run claude-lean in {effort}-effort print mode with terminal marker filtering."
        ),
    )
    parser.add_argument("--model", help="Claude model alias or full name to pass through")
    args = parser.parse_args(argv)
    return cast(str | None, args.model)


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return cast(Mapping[str, object], value)


def _tool_use_line(block: Mapping[str, object]) -> str:
    name = block.get("name")
    rendered_name = name if isinstance(name, str) and name else "unknown"
    rendered = f"tool_use {rendered_name}"
    tool_input = _as_mapping(block.get("input"))
    if tool_input is None:
        return f"{rendered}\n"

    for key in ("command", "file_path", "url", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            summary = " ".join(value.split())
            if len(summary) > _SUMMARY_LIMIT:
                summary = f"{summary[: _SUMMARY_LIMIT - 1]}…"
            return f"{rendered}: {key}={summary}\n"
    return f"{rendered}\n"


def _assistant_diagnostics(event: Mapping[str, object]) -> list[str]:
    message = _as_mapping(event.get("message"))
    if message is None:
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []

    diagnostics: list[str] = []
    for value in cast(list[object], content):
        block = _as_mapping(value)
        if block is None:
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                diagnostics.append(text if text.endswith("\n") else f"{text}\n")
        elif block_type == "tool_use":
            diagnostics.append(_tool_use_line(block))
    return diagnostics


def _record_result(event: Mapping[str, object], final_result: _FinalResult) -> None:
    result = event.get("result")
    subtype = event.get("subtype")
    final_result.seen = True
    final_result.text = result if isinstance(result, str) else None
    final_result.is_error = event.get("is_error") is True or (
        isinstance(subtype, str) and subtype.startswith("error")
    )


def _stream_claude_output(
    source: TextIO,
    target: TextIO,
    failures: Queue[BaseException],
    final_result: _FinalResult,
) -> None:
    try:
        marker_filter = TerminalMarkerFilter()
        renderer = _DiagnosticRenderer(target)
        while True:
            line = source.readline()
            if line == "":
                break
            try:
                decoded: object = json.loads(line)
            except JSONDecodeError:
                renderer.write(marker_filter.filter(line))
                continue

            event = _as_mapping(decoded)
            if event is None:
                continue
            event_type = event.get("type")
            if event_type == "assistant":
                for diagnostic in _assistant_diagnostics(event):
                    renderer.write(marker_filter.filter(diagnostic))
            elif event_type == "result":
                _record_result(event, final_result)
        renderer.write(marker_filter.finish())
        renderer.finish()
    except BaseException as error:
        failures.put(error)


def _stream_diagnostics(
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


def main_high(argv: Sequence[str] | None = None) -> int:
    """Run Claude in high-effort print mode and emit only one terminal marker."""
    return _run(argv, "high")


def main_medium(argv: Sequence[str] | None = None) -> int:
    """Run Claude in medium-effort print mode and emit only one terminal marker."""
    return _run(argv, "medium")


def _run(argv: Sequence[str] | None, effort: _Effort) -> int:
    model = _parse_model(effort, argv)
    prompt = sys.stdin.read()
    command = [
        "claude-lean",
        "--effort",
        effort,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if model is not None:
        command.extend(("--model", model))

    process: subprocess.Popen[str] | None = None
    threads: list[threading.Thread] = []
    with _managed_termination_signals() as termination_signals:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            failures: Queue[BaseException] = Queue()
            final_result = _FinalResult()
            candidate_threads = [
                threading.Thread(
                    target=_write_stdin,
                    args=(process.stdin, prompt),
                    daemon=True,
                ),
                threading.Thread(
                    target=_stream_claude_output,
                    args=(process.stdout, sys.stdout, failures, final_result),
                    daemon=True,
                ),
                threading.Thread(
                    target=_stream_diagnostics,
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
                raise OSError(f"failed streaming Claude output: {failure}") from failure

            return_code = process.wait()
            _join_threads(threads)
            try:
                failure = failures.get_nowait()
            except Empty:
                pass
            else:
                raise OSError(f"failed streaming Claude output: {failure}") from failure

            final_message = final_result.text or ""
            marker = None
            if final_result.seen and not final_result.is_error:
                with suppress(MarkerParseError):
                    marker = parse_terminal_marker(final_message)

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
    raise SystemExit(main_high())
