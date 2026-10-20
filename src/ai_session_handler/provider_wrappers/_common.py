"""Shared process and diagnostic helpers for bundled provider wrappers."""

from __future__ import annotations

import signal
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from types import FrameType
from typing import Final, NoReturn, TextIO

_CHILD_GRACE_SECONDS: Final[float] = 0.5
_THREAD_JOIN_SECONDS: Final[float] = 1.0


class DeferredTerminationSignals:
    """Remember the first catchable termination signal until cleanup is safe."""

    def __init__(self) -> None:
        self._pending_signal: int | None = None

    def handle(self, signal_number: int, frame: FrameType | None) -> None:
        del frame
        if self._pending_signal is None:
            self._pending_signal = signal_number

    def raise_if_pending(self) -> None:
        if self._pending_signal == signal.SIGINT:
            _raise_keyboard_interrupt(signal.SIGINT, None)
        if self._pending_signal is not None:
            _raise_system_exit(self._pending_signal, None)


class DiagnosticRenderer:
    """Render prefixed diagnostics without affecting terminal marker parsing."""

    def __init__(self, target: TextIO, prefix: str) -> None:
        self._target = target
        self._prefix = prefix
        self._at_line_boundary = True

    def write(self, text: str) -> None:
        rendered: list[str] = []
        for character in text:
            if self._at_line_boundary:
                rendered.append(self._prefix)
                self._at_line_boundary = False
            rendered.append("&lt;" if character == "<" else character)
            if character == "\n":
                self._at_line_boundary = True
        if rendered:
            self._target.write("".join(rendered))
            self._target.flush()

    def finish(self) -> None:
        if not self._at_line_boundary:
            self._target.write("\n")
            self._target.flush()
            self._at_line_boundary = True


def write_stdin(stream: TextIO, prompt: str) -> None:
    """Write a prompt to a child and tolerate an early pipe close."""
    try:
        stream.write(prompt)
    except (BrokenPipeError, OSError, ValueError):
        return
    finally:
        with suppress(BrokenPipeError, OSError, ValueError):
            stream.close()


def terminate_child(process: subprocess.Popen[str]) -> None:
    """Terminate a child, escalating to a kill after a short grace period."""
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


def close_process_pipes(process: subprocess.Popen[str]) -> None:
    """Close every pipe owned by a child process."""
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with suppress(BrokenPipeError, OSError, ValueError):
                stream.close()


def join_threads(threads: Sequence[threading.Thread]) -> None:
    """Bound how long cleanup waits for each started helper thread."""
    for thread in threads:
        thread.join(timeout=_THREAD_JOIN_SECONDS)


@contextmanager
def managed_termination_signals() -> Iterator[DeferredTerminationSignals]:
    """Defer SIGINT and SIGTERM while a wrapper owns child cleanup."""
    deferred = DeferredTerminationSignals()
    if threading.current_thread() is not threading.main_thread():
        yield deferred
        return

    handled_signals = (signal.SIGINT, signal.SIGTERM)
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)
    previous_handlers = {
        signal_number: signal.getsignal(signal_number) for signal_number in handled_signals
    }
    try:
        for signal_number in handled_signals:
            signal.signal(signal_number, deferred.handle)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    try:
        yield deferred
    finally:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        deferred.raise_if_pending()


def _raise_keyboard_interrupt(signal_number: int, frame: FrameType | None) -> NoReturn:
    del signal_number, frame
    raise KeyboardInterrupt


def _raise_system_exit(signal_number: int, frame: FrameType | None) -> NoReturn:
    del frame
    raise SystemExit(128 + signal_number)
