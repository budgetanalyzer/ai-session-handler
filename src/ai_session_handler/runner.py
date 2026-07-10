"""Runner orchestration for one or more explicit plan phases."""

from __future__ import annotations

import math
import re
import shlex
import string
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Final, TextIO

from ai_session_handler.markers import (
    MarkerKind,
    MissingMarkerError,
    MultipleMarkersError,
    TerminalMarkerFilter,
    parse_terminal_marker,
)
from ai_session_handler.phases import Phase, parse_phase_file
from ai_session_handler.prompts import PromptContext, render_worker_prompt, write_worker_prompt
from ai_session_handler.state import (
    LastRun,
    RunnerState,
    StopReason,
    StopState,
    ensure_plan_hash_matches,
    format_utc_timestamp,
    read_state,
    select_next_phase,
    with_current_phase,
    write_state,
)
from ai_session_handler.transcripts import (
    TranscriptHeader,
    render_transcript_header,
    transcript_path,
)

EXIT_OK: Final[int] = 0
EXIT_BLOCKED: Final[int] = 2
EXIT_NEEDS_CLARIFICATION: Final[int] = 3
EXIT_AGENT_FAILED: Final[int] = 4
EXIT_INVALID: Final[int] = 5

_SUPPORTED_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {"prompt_file", "workspace", "run_id", "transcript_file", "state_file"}
)


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Inputs for executing one or more plan phases."""

    workspace_path: Path
    plan_path: Path
    state_path: Path
    agent_cmd: str
    max_phases: int = 1
    timeout_seconds: float | None = 3600.0
    stop_on_regex: tuple[str, ...] = ()
    retry_stopped: bool = False
    accept_plan_change: bool = False


@dataclass(frozen=True, slots=True)
class RunnerOutcome:
    """User-facing result from a runner invocation."""

    exit_code: int
    message: str
    state: RunnerState


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Captured process execution details."""

    exit_code: int
    combined_output: str
    started_at: str
    finished_at: str
    transcript_path: Path
    stop_reason: StopReason | None = None
    stop_message: str | None = None


@dataclass(frozen=True, slots=True)
class _StreamItem:
    stream_name: str
    text: str


class CommandTemplateError(ValueError):
    """Raised when an agent command template is invalid."""


def run_phases(options: RunOptions) -> RunnerOutcome:
    """Run selected plan phases and persist state transitions."""
    if options.max_phases < 1:
        raise ValueError("max_phases must be at least 1")
    if options.timeout_seconds is not None and (
        not math.isfinite(options.timeout_seconds) or options.timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite number greater than 0")
    stop_patterns = _compile_stop_patterns(options.stop_on_regex)

    phases = parse_phase_file(options.plan_path)
    state = read_state(options.state_path)
    state = ensure_plan_hash_matches(
        state,
        options.plan_path,
        phases,
        accept_plan_change=options.accept_plan_change,
    )

    outcome: RunnerOutcome
    retry_stopped = options.retry_stopped
    for _ in range(options.max_phases):
        phase = select_next_phase(state, phases, retry_stopped=retry_stopped)
        retry_stopped = False
        if phase is None:
            state = with_current_phase(replace(state, stop=None), None)
            write_state(options.state_path, state)
            return RunnerOutcome(EXIT_OK, "runner-complete: all phases complete", state)

        state = with_current_phase(replace(state, stop=None), phase)
        write_state(options.state_path, state)

        run_id = create_run_id(phase)
        current_transcript_path = transcript_path(options.state_path.parent, run_id)
        prompt_context = PromptContext(
            workspace_path=options.workspace_path,
            plan_path=options.plan_path,
            state_path=options.state_path,
            phase=phase,
            state=state,
            run_id=run_id,
            transcript_path=current_transcript_path,
        )
        prompt_path = write_worker_prompt(options.state_path.parent, prompt_context)
        prompt_text = render_worker_prompt(prompt_context)

        process_result = run_agent_process(
            agent_cmd=options.agent_cmd,
            prompt_text=prompt_text,
            prompt_path=prompt_path,
            workspace_path=options.workspace_path,
            run_id=run_id,
            transcript_file=current_transcript_path,
            state_file=options.state_path,
            phase=phase,
            plan_path=options.plan_path,
            timeout_seconds=options.timeout_seconds,
            stop_patterns=stop_patterns,
        )

        state, outcome = apply_process_result(state, phase, run_id, process_result)
        write_state(options.state_path, state)

        if outcome.exit_code != EXIT_OK:
            return outcome

    return outcome


def create_run_id(phase: Phase, *, timestamp: datetime | None = None) -> str:
    """Create a stable run id from a UTC timestamp and phase id."""
    now = datetime.now(UTC) if timestamp is None else timestamp.astimezone(UTC)
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{phase.id}"


def run_agent_process(
    *,
    agent_cmd: str,
    prompt_text: str,
    prompt_path: Path,
    workspace_path: Path,
    run_id: str,
    transcript_file: Path,
    state_file: Path,
    phase: Phase,
    plan_path: Path,
    timeout_seconds: float | None,
    stop_patterns: Sequence[re.Pattern[str]],
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> ProcessResult:
    """Execute the agent command, streaming output while capturing a transcript."""
    started_at = format_utc_timestamp()
    transcript_file.parent.mkdir(parents=True, exist_ok=True)
    command = render_command_template(
        agent_cmd,
        prompt_file=prompt_path,
        workspace=workspace_path,
        run_id=run_id,
        transcript_file=transcript_file,
        state_file=state_file,
    )
    output_queue: Queue[_StreamItem] = Queue()
    combined_parts: list[str] = []
    process = subprocess.Popen(
        command,
        cwd=workspace_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )

    assert process.stdout is not None
    assert process.stderr is not None
    threads = [
        threading.Thread(
            target=_read_stream,
            args=("stdout", process.stdout, output_queue),
            daemon=True,
        ),
        threading.Thread(
            target=_read_stream,
            args=("stderr", process.stderr, output_queue),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    timeout_at = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    assert process.stdin is not None
    stdin_thread = threading.Thread(
        target=_write_stdin,
        args=(process.stdin, prompt_text),
        daemon=True,
    )
    stdin_thread.start()

    stop_reason: StopReason | None = None
    stop_message: str | None = None
    stdout_target = sys.stdout if stdout is None else stdout
    stderr_target = sys.stderr if stderr is None else stderr
    display_filters = {
        "stdout": TerminalMarkerFilter(),
        "stderr": TerminalMarkerFilter(),
    }

    header = TranscriptHeader(
        run_id=run_id,
        phase_id=phase.id,
        phase_title=phase.title,
        plan_path=plan_path,
        state_path=state_file,
        workspace_path=workspace_path,
        started_at=started_at,
        agent_cmd=agent_cmd,
        rendered_command=command,
    )
    with transcript_file.open("w", encoding="utf-8") as transcript:
        transcript.write(render_transcript_header(header))
        while True:
            _drain_output_queue(
                output_queue,
                transcript,
                combined_parts,
                stdout_target,
                stderr_target,
                display_filters,
            )
            if stop_reason is None:
                matched_pattern = _first_matching_pattern(stop_patterns, combined_parts)
                if matched_pattern is not None:
                    stop_reason = StopReason.STOP_REGEX
                    stop_message = f"output matched stop regex: {matched_pattern.pattern}"
                    _terminate_process(process)

            if stop_reason is None and timeout_at is not None and time.monotonic() >= timeout_at:
                stop_reason = StopReason.TIMEOUT
                stop_message = f"agent command timed out after {timeout_seconds:g} seconds"
                _terminate_process(process)

            if process.poll() is not None:
                break

            try:
                item = output_queue.get(timeout=0.05)
            except Empty:
                continue
            _write_stream_item(
                item,
                transcript,
                combined_parts,
                stdout_target,
                stderr_target,
                display_filters,
            )

        return_code = process.wait()
        for thread in (*threads, stdin_thread):
            thread.join(timeout=1)
        _drain_output_queue(
            output_queue,
            transcript,
            combined_parts,
            stdout_target,
            stderr_target,
            display_filters,
        )
        if not combined_parts:
            transcript.write(
                f"[runner] process exited with code {return_code} without stdout/stderr output\n"
            )

    finished_at = format_utc_timestamp()
    return ProcessResult(
        exit_code=return_code,
        combined_output="".join(combined_parts),
        started_at=started_at,
        finished_at=finished_at,
        transcript_path=transcript_file,
        stop_reason=stop_reason,
        stop_message=stop_message,
    )


def render_command_template(
    template: str,
    *,
    prompt_file: Path,
    workspace: Path,
    run_id: str,
    transcript_file: Path,
    state_file: Path,
) -> list[str]:
    """Substitute supported placeholders and split the command without a shell."""
    formatter = string.Formatter()
    fields = [
        field_name
        for _, field_name, _, _ in formatter.parse(template)
        if field_name is not None and field_name != ""
    ]
    unsupported = sorted(set(fields) - _SUPPORTED_PLACEHOLDERS)
    if unsupported:
        joined = ", ".join(unsupported)
        raise CommandTemplateError(f"unsupported command placeholder(s): {joined}")

    substitutions = {
        "prompt_file": str(prompt_file),
        "workspace": str(workspace),
        "run_id": run_id,
        "transcript_file": str(transcript_file),
        "state_file": str(state_file),
    }
    try:
        template_args = shlex.split(template)
    except ValueError as error:
        raise CommandTemplateError(f"invalid command template quoting: {error}") from error

    command = [argument.format(**substitutions) for argument in template_args]
    if not command:
        raise CommandTemplateError("agent command template produced an empty command")
    return command


def apply_process_result(
    state: RunnerState,
    phase: Phase,
    run_id: str,
    result: ProcessResult,
) -> tuple[RunnerState, RunnerOutcome]:
    """Apply process and marker results to durable state."""
    if result.stop_reason is not None:
        return _stopped_runner_failure(state, phase, run_id, result, result.stop_reason)

    if result.exit_code != 0:
        return _stopped_runner_failure(state, phase, run_id, result, StopReason.AGENT_FAILED)

    try:
        marker = parse_terminal_marker(result.combined_output)
    except MissingMarkerError:
        return _stopped_runner_failure(state, phase, run_id, result, StopReason.MISSING_MARKER)
    except MultipleMarkersError:
        return _stopped_runner_failure(state, phase, run_id, result, StopReason.MULTIPLE_MARKERS)

    if marker.kind is MarkerKind.COMPLETE:
        completed = state.completed_phase_ids
        if phase.id not in completed:
            completed = (*completed, phase.id)
        updated = replace(
            state,
            completed_phase_ids=completed,
            current_phase=None,
            stop=None,
            last_run=_last_run(
                run_id,
                phase.id,
                "phase-complete",
                result,
                marker.text,
                EXIT_OK,
            ),
        )
        return updated, RunnerOutcome(EXIT_OK, f"phase-complete: {phase.id}", updated)

    if marker.kind is MarkerKind.BLOCKED:
        updated = replace(
            state,
            stop=StopState(reason=StopReason.BLOCKED, phase_id=phase.id, message=marker.text),
            last_run=_last_run(run_id, phase.id, "blocked", result, marker.text, EXIT_BLOCKED),
        )
        return updated, RunnerOutcome(EXIT_BLOCKED, f"phase-blocked: {marker.text}", updated)

    updated = replace(
        state,
        stop=StopState(
            reason=StopReason.NEEDS_CLARIFICATION,
            phase_id=phase.id,
            clarification_request=marker.text,
        ),
        last_run=_last_run(
            run_id,
            phase.id,
            "needs-clarification",
            result,
            marker.text,
            EXIT_NEEDS_CLARIFICATION,
        ),
    )
    return (
        updated,
        RunnerOutcome(
            EXIT_NEEDS_CLARIFICATION, f"phase-needs-clarification: {marker.text}", updated
        ),
    )


def _stopped_runner_failure(
    state: RunnerState,
    phase: Phase,
    run_id: str,
    result: ProcessResult,
    reason: StopReason,
) -> tuple[RunnerState, RunnerOutcome]:
    message = result.stop_message or _failure_message(reason, result.exit_code)
    updated = replace(
        state,
        stop=StopState(reason=reason, phase_id=phase.id, message=message),
        last_run=_last_run(run_id, phase.id, reason.value, result, message, EXIT_AGENT_FAILED),
    )
    return updated, RunnerOutcome(EXIT_AGENT_FAILED, f"{reason.value}: {message}", updated)


def _last_run(
    run_id: str,
    phase_id: str,
    status: str,
    result: ProcessResult,
    summary: str,
    exit_code: int,
) -> LastRun:
    return LastRun(
        run_id=run_id,
        phase_id=phase_id,
        status=status,
        started_at=result.started_at,
        finished_at=result.finished_at,
        exit_code=exit_code,
        transcript_path=str(result.transcript_path),
        summary=summary,
    )


def _failure_message(reason: StopReason, process_exit_code: int) -> str:
    if reason is StopReason.AGENT_FAILED:
        return f"agent command exited with code {process_exit_code}"
    if reason is StopReason.MISSING_MARKER:
        return "agent output did not contain a terminal marker"
    if reason is StopReason.MULTIPLE_MARKERS:
        return "agent output contained multiple terminal markers"
    return reason.value


def _read_stream(stream_name: str, stream: TextIO, output_queue: Queue[_StreamItem]) -> None:
    while True:
        chunk = stream.readline()
        if chunk == "":
            break
        output_queue.put(_StreamItem(stream_name=stream_name, text=chunk))


def _write_stdin(stream: TextIO, prompt_text: str) -> None:
    try:
        stream.write(prompt_text)
    except (BrokenPipeError, OSError, ValueError):
        return
    finally:
        with suppress(BrokenPipeError, OSError, ValueError):
            stream.close()


def _drain_output_queue(
    output_queue: Queue[_StreamItem],
    transcript: TextIO,
    combined_parts: list[str],
    stdout: TextIO,
    stderr: TextIO,
    display_filters: dict[str, TerminalMarkerFilter],
) -> None:
    while True:
        try:
            item = output_queue.get_nowait()
        except Empty:
            return
        _write_stream_item(item, transcript, combined_parts, stdout, stderr, display_filters)


def _write_stream_item(
    item: _StreamItem,
    transcript: TextIO,
    combined_parts: list[str],
    stdout: TextIO,
    stderr: TextIO,
    display_filters: dict[str, TerminalMarkerFilter],
) -> None:
    combined_parts.append(item.text)
    target = stdout if item.stream_name == "stdout" else stderr
    display_text = display_filters[item.stream_name].filter(item.text)
    if display_text:
        target.write(display_text)
        target.flush()
    transcript.write(item.text)
    transcript.flush()


def _first_matching_pattern(
    stop_patterns: Sequence[re.Pattern[str]],
    combined_parts: Sequence[str],
) -> re.Pattern[str] | None:
    if not stop_patterns or not combined_parts:
        return None
    output = "".join(combined_parts)
    for pattern in stop_patterns:
        if pattern.search(output) is not None:
            return pattern
    return None


def _compile_stop_patterns(stop_on_regex: Sequence[str]) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    for pattern in stop_on_regex:
        try:
            patterns.append(re.compile(pattern))
        except re.error as error:
            raise ValueError(f"invalid stop regex {pattern!r}: {error}") from error
    return patterns


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
