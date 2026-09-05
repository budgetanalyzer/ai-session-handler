"""Codex high-reasoning exec wrapper with terminal marker filtering."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from queue import Empty, Queue
from types import FrameType
from typing import Final, NoReturn, TextIO, cast

from ai_session_handler.markers import (
    MarkerKind,
    MarkerParseError,
    TerminalMarkerFilter,
    parse_terminal_marker,
)

_CHILD_GRACE_SECONDS: Final[float] = 0.5
_THREAD_JOIN_SECONDS: Final[float] = 1.0


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


def _stream_filtered_output(
    source: TextIO,
    target: TextIO,
    failures: Queue[BaseException],
) -> None:
    try:
        marker_filter = TerminalMarkerFilter()
        while True:
            chunk = source.readline()
            if chunk == "":
                break
            visible_chunk = _sanitize_markers(marker_filter.filter(chunk))
            if visible_chunk:
                target.write(visible_chunk)
                target.flush()
        trailing_text = _sanitize_markers(marker_filter.finish())
        if trailing_text:
            target.write(trailing_text)
            target.flush()
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
        threads: list[threading.Thread] = []
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            failures: Queue[BaseException] = Queue()
            threads = [
                threading.Thread(target=_write_stdin, args=(process.stdin, prompt), daemon=True),
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
            for thread in threads:
                thread.start()

            with _managed_termination_signals():
                while process.poll() is None:
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
                sys.stdout.write(f"\n<{tag}>{marker.text}</{tag}>\n")
            elif final_message.strip():
                sys.stdout.write(_sanitize_markers(final_message))
                if not final_message.endswith("\n"):
                    sys.stdout.write("\n")

            return return_code
        finally:
            _terminate_child(process)
            _close_process_pipes(process)
            _join_threads(threads)


def _terminate_child(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    deadline = time.monotonic() + _CHILD_GRACE_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if process.poll() is None:
        process.kill()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_CHILD_GRACE_SECONDS)


def _close_process_pipes(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with suppress(BrokenPipeError, OSError, ValueError):
                stream.close()


def _join_threads(threads: Sequence[threading.Thread]) -> None:
    for thread in threads:
        thread.join(timeout=_THREAD_JOIN_SECONDS)


@contextmanager
def _managed_termination_signals() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    handled_signals = (signal.SIGINT, signal.SIGTERM)
    previous_handlers = {
        signal_number: signal.getsignal(signal_number) for signal_number in handled_signals
    }
    try:
        signal.signal(signal.SIGINT, _raise_keyboard_interrupt)
        signal.signal(signal.SIGTERM, _raise_system_exit)
        yield
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)


def _raise_keyboard_interrupt(signal_number: int, frame: FrameType | None) -> NoReturn:
    del signal_number, frame
    raise KeyboardInterrupt


def _raise_system_exit(signal_number: int, frame: FrameType | None) -> NoReturn:
    del frame
    raise SystemExit(128 + signal_number)


if __name__ == "__main__":
    raise SystemExit(main())
