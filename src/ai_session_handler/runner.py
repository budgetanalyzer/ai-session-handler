"""Runner orchestration for one or more explicit plan phases."""

from __future__ import annotations

import math
import os
import re
import shlex
import signal
import string
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from errno import EACCES, EAGAIN
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from pathlib import Path
from queue import Empty, Queue
from types import FrameType
from typing import Final, NoReturn, TextIO
from uuid import uuid4

from ai_session_handler.artifacts import open_text_exclusively
from ai_session_handler.markers import (
    InvalidMarkerError,
    MarkerKind,
    MissingMarkerError,
    MultipleMarkersError,
    TerminalMarkerFilter,
    parse_terminal_marker,
)
from ai_session_handler.outcomes import (
    OutcomeArtifacts,
    OutcomeRecord,
    outcome_path,
    write_outcome,
)
from ai_session_handler.phases import Phase, read_plan_snapshot, resolve_phase_workspace
from ai_session_handler.prompts import PromptContext, render_worker_prompt, write_worker_prompt
from ai_session_handler.state import (
    ActiveAttempt,
    ActiveAttemptStatus,
    ActiveWorkerError,
    AttemptStatus,
    LastRun,
    OutcomeRef,
    PhaseRef,
    ProcessIdentity,
    RunnerState,
    SnapshotIdentity,
    StopReason,
    StopState,
    ensure_plan_hash_matches,
    ensure_plan_snapshot_unchanged,
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
_PROCESS_GROUP_GRACE_SECONDS: Final[float] = 0.5
_THREAD_JOIN_SECONDS: Final[float] = 1.0


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Inputs for executing one or more plan phases."""

    plan_workspace_path: Path
    plan_path: Path
    state_path: Path
    agent_cmd: str
    max_phases: int | None = None
    timeout_seconds: float | None = 3600.0
    stop_on_regex: tuple[str, ...] = ()
    retry_stopped: bool = False
    accept_plan_change: bool = False
    quiet: bool = False


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
    stdout_output: str
    stderr_output: str
    started_at: str
    finished_at: str
    transcript_path: Path
    stop_reason: StopReason | None = None
    stop_message: str | None = None


@dataclass(frozen=True, slots=True)
class _StreamItem:
    stream_name: str
    text: str


@dataclass(frozen=True, slots=True)
class _StreamFailure:
    stream_name: str
    error: BaseException


type _OutputQueueItem = _StreamItem | _StreamFailure


@dataclass(frozen=True, slots=True)
class _DirectoryOwnership:
    device: int
    inode: int

    def owns(self, path: Path) -> bool:
        directory = path.stat()
        return (directory.st_dev, directory.st_ino) == (self.device, self.inode)


class CommandTemplateError(ValueError):
    """Raised when an agent command template is invalid."""


class ExecutionOwnershipError(RuntimeError):
    """Raised when another handler invocation owns a required directory."""


class AgentLaunchError(OSError):
    """Raised when a prepared attempt cannot launch its worker command."""


def run_phases(options: RunOptions) -> RunnerOutcome:
    """Run selected plan phases and persist state transitions."""
    if options.max_phases is not None and options.max_phases < 1:
        raise ValueError("max_phases must be at least 1")
    if options.timeout_seconds is not None and (
        not math.isfinite(options.timeout_seconds) or options.timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite number greater than 0")
    stop_patterns = _compile_stop_patterns(options.stop_on_regex)
    _parse_command_template(options.agent_cmd)

    snapshot = read_plan_snapshot(options.plan_path)
    with _own_directory(options.plan_workspace_path, scope="plan workspace") as plan_ownership:
        phases = snapshot.phases
        state = read_state(options.state_path)
        if state.active_attempt is not None and not options.retry_stopped:
            select_next_phase(state, phases)
        state = ensure_plan_hash_matches(
            state,
            snapshot,
            accept_plan_change=options.accept_plan_change,
        )

        retry_stopped = options.retry_stopped
        phases_run = 0
        while True:
            if phases_run > 0:
                ensure_plan_snapshot_unchanged(snapshot)
            phase = select_next_phase(state, phases, retry_stopped=retry_stopped)
            abandoned_attempt = state.active_attempt if retry_stopped else None
            retry_stopped = False
            if phase is None:
                if phases_run == 0:
                    ensure_plan_snapshot_unchanged(snapshot)
                state = with_current_phase(replace(state, stop=None), None)
                write_state(options.state_path, state)
                return RunnerOutcome(EXIT_OK, "runner-complete: all phases complete", state)

            execution_workspace_path = resolve_phase_workspace(
                phase,
                plan_workspace_path=options.plan_workspace_path,
                source=str(snapshot.path),
            )
            if (
                abandoned_attempt is not None
                and Path(abandoned_attempt.execution_workspace).resolve()
                != execution_workspace_path
            ):
                raise ValueError(
                    "active attempt execution workspace differs from the selected phase; "
                    "restore the original workspace declaration before retrying"
                )
            with _own_execution_workspace(plan_ownership, execution_workspace_path):
                if abandoned_attempt is not None and _active_worker_is_alive(abandoned_attempt):
                    raise ActiveWorkerError(abandoned_attempt)
                run_id = create_run_id(phase)
                current_transcript_path = transcript_path(options.state_path.parent, run_id)
                current_prompt_path = options.state_path.parent / "prompts" / f"{run_id}.txt"
                current_outcome_path = outcome_path(options.state_path.parent, run_id)
                if abandoned_attempt is not None:
                    state = _resolve_abandoned_attempt(state, abandoned_attempt)
                active_attempt = ActiveAttempt(
                    id=run_id,
                    status=ActiveAttemptStatus.PREPARED,
                    phase=PhaseRef(id=phase.id, title=phase.title),
                    snapshot=SnapshotIdentity(path=str(snapshot.path), sha256=snapshot.sha256),
                    execution_workspace=str(execution_workspace_path),
                    started_at=format_utc_timestamp(),
                    prompt_path=str(current_prompt_path),
                    transcript_path=str(current_transcript_path),
                    outcome_path=str(current_outcome_path),
                )
                state = replace(
                    state,
                    current_phase=active_attempt.phase,
                    active_attempt=active_attempt,
                    stop=None,
                )
                try:
                    write_state(options.state_path, state)
                except OSError as error:
                    return RunnerOutcome(
                        EXIT_AGENT_FAILED,
                        f"execution-io-failed: could not prepare attempt state: {error}",
                        state,
                    )
                prompt_context = PromptContext(
                    plan_workspace_path=options.plan_workspace_path,
                    execution_workspace_path=execution_workspace_path,
                    plan_path=snapshot.path,
                    state_path=options.state_path,
                    phase=phase,
                    state=state,
                    run_id=run_id,
                    transcript_path=current_transcript_path,
                    plan_preamble=snapshot.preamble,
                )
                try:
                    prompt_path = write_worker_prompt(options.state_path.parent, prompt_context)
                    prompt_text = render_worker_prompt(prompt_context)

                    def record_process(
                        process_identity: ProcessIdentity | None,
                        prepared_attempt: ActiveAttempt = active_attempt,
                    ) -> None:
                        nonlocal state
                        running_attempt = replace(
                            prepared_attempt,
                            status=ActiveAttemptStatus.RUNNING,
                            process=process_identity,
                        )
                        running_state = replace(state, active_attempt=running_attempt)
                        write_state(options.state_path, running_state)
                        state = running_state

                    process_result = run_agent_process(
                        agent_cmd=options.agent_cmd,
                        prompt_text=prompt_text,
                        prompt_path=prompt_path,
                        plan_workspace_path=options.plan_workspace_path,
                        execution_workspace_path=execution_workspace_path,
                        run_id=run_id,
                        transcript_file=current_transcript_path,
                        state_file=options.state_path,
                        phase=phase,
                        plan_path=snapshot.path,
                        timeout_seconds=options.timeout_seconds,
                        stop_patterns=stop_patterns,
                        quiet=options.quiet,
                        started_at=active_attempt.started_at,
                        process_started=record_process,
                    )
                except AgentLaunchError as error:
                    return _record_attempt_failure(
                        options.state_path,
                        state,
                        StopReason.LAUNCH_FAILED,
                        f"could not launch agent command: {error}",
                    )
                except (KeyboardInterrupt, SystemExit):
                    interrupted = _attempt_failure_state(
                        state,
                        StopReason.INTERRUPTED,
                        "handler interrupted while the worker was active",
                    )
                    with suppress(OSError):
                        _write_transition_outcome(state, interrupted)
                        write_state(options.state_path, interrupted)
                    raise
                except OSError as error:
                    return _record_attempt_failure(
                        options.state_path,
                        state,
                        StopReason.EXECUTION_IO_FAILED,
                        f"attempt execution IO failed: {error}",
                    )

                updated_state, outcome = apply_process_result(state, phase, process_result)
                try:
                    _write_transition_outcome(state, updated_state)
                except OSError as error:
                    return RunnerOutcome(
                        EXIT_AGENT_FAILED,
                        f"execution-io-failed: could not write attempt outcome: {error}",
                        state,
                    )
                try:
                    write_state(options.state_path, updated_state)
                except OSError as error:
                    return RunnerOutcome(
                        EXIT_AGENT_FAILED,
                        f"execution-io-failed: could not record attempt outcome: {error}",
                        state,
                    )
                state = updated_state

            if outcome.exit_code != EXIT_OK:
                return outcome

            phases_run += 1
            if options.max_phases is not None and phases_run >= options.max_phases:
                return outcome


@contextmanager
def _own_directory(path: Path, *, scope: str) -> Iterator[_DirectoryOwnership]:
    canonical_path = path.resolve()
    descriptor = os.open(canonical_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.set_inheritable(descriptor, False)
        try:
            flock(descriptor, LOCK_EX | LOCK_NB)
        except OSError as error:
            if error.errno not in {EACCES, EAGAIN}:
                raise
            raise ExecutionOwnershipError(
                f"{scope} {canonical_path} is already owned by another "
                "ai-session-handler invocation"
            ) from error

        directory = os.fstat(descriptor)
        yield _DirectoryOwnership(
            device=directory.st_dev,
            inode=directory.st_ino,
        )
    finally:
        with suppress(OSError):
            flock(descriptor, LOCK_UN)
        os.close(descriptor)


@contextmanager
def _own_execution_workspace(
    plan_ownership: _DirectoryOwnership,
    execution_workspace_path: Path,
) -> Iterator[None]:
    if plan_ownership.owns(execution_workspace_path):
        yield
        return

    with _own_directory(execution_workspace_path, scope="execution workspace"):
        yield


def create_run_id(phase: Phase, *, timestamp: datetime | None = None) -> str:
    """Create a human-readable, UUID-unique attempt id."""
    now = datetime.now(UTC) if timestamp is None else timestamp.astimezone(UTC)
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{phase.id}-{uuid4()}"


def run_agent_process(
    *,
    agent_cmd: str,
    prompt_text: str,
    prompt_path: Path,
    plan_workspace_path: Path,
    execution_workspace_path: Path,
    run_id: str,
    transcript_file: Path,
    state_file: Path,
    phase: Phase,
    plan_path: Path,
    timeout_seconds: float | None,
    stop_patterns: Sequence[re.Pattern[str]],
    quiet: bool = False,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    started_at: str | None = None,
    process_started: Callable[[ProcessIdentity | None], None] | None = None,
) -> ProcessResult:
    """Execute the agent command, capturing output and optionally streaming it live."""
    process_started_at = format_utc_timestamp() if started_at is None else started_at
    command = render_command_template(
        agent_cmd,
        prompt_file=prompt_path,
        workspace=execution_workspace_path,
        run_id=run_id,
        transcript_file=transcript_file,
        state_file=state_file,
    )
    output_queue: Queue[_OutputQueueItem] = Queue()
    combined_parts: list[str] = []
    stream_parts: dict[str, list[str]] = {"stdout": [], "stderr": []}
    transcript = open_text_exclusively(transcript_file)
    stdout_target = None if quiet else (sys.stdout if stdout is None else stdout)
    stderr_target = None if quiet else (sys.stderr if stderr is None else stderr)
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
        plan_workspace_path=plan_workspace_path,
        execution_workspace_path=execution_workspace_path,
        started_at=process_started_at,
        agent_cmd=agent_cmd,
        rendered_command=command,
    )
    process: subprocess.Popen[str] | None = None
    process_group_id: int | None = None
    threads: list[threading.Thread] = []
    lifecycle_complete = False
    stop_reason: StopReason | None = None
    stop_message: str | None = None
    return_code: int

    with transcript:
        transcript.write(render_transcript_header(header))
        transcript.flush()
        try:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=execution_workspace_path,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                    start_new_session=True,
                )
            except OSError as error:
                raise AgentLaunchError(str(error)) from error
            process_group_id = process.pid
            if process_started is not None:
                process_started(_read_process_identity(process.pid))
            assert process.stdin is not None
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
                threading.Thread(
                    target=_write_stdin,
                    args=(process.stdin, prompt_text),
                    daemon=True,
                ),
            ]
            for thread in threads:
                thread.start()

            timeout_at = None if timeout_seconds is None else time.monotonic() + timeout_seconds
            with _managed_termination_signals():
                while True:
                    _drain_output_queue(
                        output_queue,
                        transcript,
                        combined_parts,
                        stream_parts,
                        stdout_target,
                        stderr_target,
                        display_filters,
                    )
                    if stop_reason is None:
                        matched_pattern = _first_matching_pattern(stop_patterns, combined_parts)
                        if matched_pattern is not None:
                            stop_reason = StopReason.STOP_REGEX
                            stop_message = f"output matched stop regex: {matched_pattern.pattern}"
                            _terminate_process_group(process, process_group_id)

                    if (
                        stop_reason is None
                        and timeout_at is not None
                        and time.monotonic() >= timeout_at
                    ):
                        stop_reason = StopReason.TIMEOUT
                        stop_message = f"agent command timed out after {timeout_seconds:g} seconds"
                        _terminate_process_group(process, process_group_id)

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
                        stream_parts,
                        stdout_target,
                        stderr_target,
                        display_filters,
                    )

            return_code = process.wait()
            _terminate_process_group(process, process_group_id)
            _join_threads(threads)
            _drain_output_queue(
                output_queue,
                transcript,
                combined_parts,
                stream_parts,
                stdout_target,
                stderr_target,
                display_filters,
            )
            _finish_display_filters(display_filters, stdout_target, stderr_target)
            if not combined_parts:
                transcript.write(
                    f"[runner] process exited with code {return_code} "
                    "without stdout/stderr output\n"
                )
            lifecycle_complete = True
        finally:
            if process is not None and process_group_id is not None and not lifecycle_complete:
                _terminate_process_group(process, process_group_id)
            if process is not None:
                _close_process_pipes(process)
            _join_threads(threads)

    finished_at = format_utc_timestamp()
    return ProcessResult(
        exit_code=return_code,
        combined_output="".join(combined_parts),
        stdout_output="".join(stream_parts["stdout"]),
        stderr_output="".join(stream_parts["stderr"]),
        started_at=process_started_at,
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
    template_args = _parse_command_template(template)
    substitutions = {
        "prompt_file": str(prompt_file),
        "workspace": str(workspace),
        "run_id": run_id,
        "transcript_file": str(transcript_file),
        "state_file": str(state_file),
    }
    return [argument.format_map(substitutions) for argument in template_args]


def _parse_command_template(template: str) -> list[str]:
    try:
        template_args = shlex.split(template)
    except ValueError as error:
        raise CommandTemplateError(f"invalid command template quoting: {error}") from error

    if not template_args or template_args[0] == "":
        raise CommandTemplateError("agent command template produced an empty command")

    formatter = string.Formatter()
    for argument in template_args:
        try:
            fields = tuple(formatter.parse(argument))
        except ValueError as error:
            raise CommandTemplateError(f"invalid command template syntax: {error}") from error

        parsed_fields = (
            (field_name, format_spec, conversion)
            for _, field_name, format_spec, conversion in fields
            if field_name is not None
        )
        raw_fields = _replacement_fields(argument)
        for (field_name, format_spec, conversion), raw_field in zip(
            parsed_fields, raw_fields, strict=True
        ):
            placeholder = f"{{{raw_field}}}"
            if field_name == "" or field_name.isdecimal():
                raise CommandTemplateError(
                    f"positional command placeholder is not allowed: {placeholder}"
                )
            if "." in field_name or "[" in field_name or "]" in field_name:
                raise CommandTemplateError(
                    "attribute and index access are not allowed in command placeholder: "
                    f"{placeholder}"
                )
            if field_name not in _SUPPORTED_PLACEHOLDERS:
                raise CommandTemplateError(f"unsupported command placeholder: {placeholder}")
            if conversion is not None:
                raise CommandTemplateError(
                    f"conversion is not allowed in command placeholder: {placeholder}"
                )
            if format_spec or ":" in raw_field:
                raise CommandTemplateError(
                    f"format specification is not allowed in command placeholder: {placeholder}"
                )

    return template_args


def _replacement_fields(argument: str) -> list[str]:
    fields: list[str] = []
    index = 0
    while index < len(argument):
        if argument[index] != "{":
            index += 1
            continue
        if index + 1 < len(argument) and argument[index + 1] == "{":
            index += 2
            continue

        start = index + 1
        index = start
        nested_braces = 0
        while index < len(argument):
            if argument[index] == "{":
                nested_braces += 1
            elif argument[index] == "}":
                if nested_braces == 0:
                    fields.append(argument[start:index])
                    index += 1
                    break
                nested_braces -= 1
            index += 1

    return fields


def apply_process_result(
    state: RunnerState,
    phase: Phase,
    result: ProcessResult,
) -> tuple[RunnerState, RunnerOutcome]:
    """Apply process and marker results to durable state."""
    attempt = state.active_attempt
    if attempt is None or attempt.phase.id != phase.id:
        raise ValueError("process result does not match the active attempt")
    if result.stop_reason is not None:
        return _stopped_runner_failure(state, phase, result, result.stop_reason)

    if result.exit_code != 0:
        return _stopped_runner_failure(state, phase, result, StopReason.AGENT_FAILED)

    try:
        marker = parse_terminal_marker(result.stdout_output, result.stderr_output)
    except MissingMarkerError:
        return _stopped_runner_failure(state, phase, result, StopReason.MISSING_MARKER)
    except MultipleMarkersError:
        return _stopped_runner_failure(state, phase, result, StopReason.MULTIPLE_MARKERS)
    except InvalidMarkerError:
        return _stopped_runner_failure(state, phase, result, StopReason.INVALID_MARKER)

    if marker.kind is MarkerKind.COMPLETE:
        completed = state.completed_phase_ids
        if phase.id not in completed:
            completed = (*completed, phase.id)
        updated = replace(
            state,
            completed_phase_ids=completed,
            current_phase=None,
            active_attempt=None,
            stop=None,
            last_run=_last_run(
                attempt,
                AttemptStatus.PHASE_COMPLETE,
                result,
                marker.text,
                EXIT_OK,
            ),
        )
        updated = _with_committed_outcome(updated)
        return updated, RunnerOutcome(EXIT_OK, f"phase-complete: {phase.id}", updated)

    if marker.kind is MarkerKind.BLOCKED:
        updated = replace(
            state,
            active_attempt=None,
            stop=StopState(reason=StopReason.BLOCKED, phase_id=phase.id, message=marker.text),
            last_run=_last_run(attempt, AttemptStatus.BLOCKED, result, marker.text, EXIT_BLOCKED),
        )
        updated = _with_committed_outcome(updated)
        return updated, RunnerOutcome(EXIT_BLOCKED, f"phase-blocked: {marker.text}", updated)

    updated = replace(
        state,
        active_attempt=None,
        stop=StopState(
            reason=StopReason.NEEDS_CLARIFICATION,
            phase_id=phase.id,
            clarification_request=marker.text,
        ),
        last_run=_last_run(
            attempt,
            AttemptStatus.NEEDS_CLARIFICATION,
            result,
            marker.text,
            EXIT_NEEDS_CLARIFICATION,
        ),
    )
    updated = _with_committed_outcome(updated)
    return (
        updated,
        RunnerOutcome(
            EXIT_NEEDS_CLARIFICATION, f"phase-needs-clarification: {marker.text}", updated
        ),
    )


def _stopped_runner_failure(
    state: RunnerState,
    phase: Phase,
    result: ProcessResult,
    reason: StopReason,
) -> tuple[RunnerState, RunnerOutcome]:
    attempt = state.active_attempt
    if attempt is None:
        raise ValueError("runner failure does not have an active attempt")
    message = result.stop_message or _failure_message(reason, result.exit_code)
    updated = replace(
        state,
        active_attempt=None,
        stop=StopState(reason=reason, phase_id=phase.id, message=message),
        last_run=_last_run(
            attempt,
            AttemptStatus(reason.value),
            result,
            message,
            EXIT_AGENT_FAILED,
        ),
    )
    updated = _with_committed_outcome(updated)
    return updated, RunnerOutcome(EXIT_AGENT_FAILED, f"{reason.value}: {message}", updated)


def _last_run(
    attempt: ActiveAttempt,
    status: AttemptStatus,
    result: ProcessResult,
    summary: str,
    exit_code: int,
) -> LastRun:
    return LastRun(
        run_id=attempt.id,
        phase_id=attempt.phase.id,
        status=status,
        started_at=result.started_at,
        finished_at=result.finished_at,
        exit_code=exit_code,
        execution_workspace=attempt.execution_workspace,
        prompt_path=attempt.prompt_path,
        transcript_path=str(result.transcript_path),
        outcome_path=attempt.outcome_path,
        summary=summary,
    )


def _attempt_failure_state(
    state: RunnerState,
    reason: StopReason,
    message: str,
) -> RunnerState:
    attempt = state.active_attempt
    if attempt is None:
        return state
    last_run = LastRun(
        run_id=attempt.id,
        phase_id=attempt.phase.id,
        status=AttemptStatus(reason.value),
        started_at=attempt.started_at,
        finished_at=format_utc_timestamp(),
        exit_code=EXIT_AGENT_FAILED,
        execution_workspace=attempt.execution_workspace,
        prompt_path=attempt.prompt_path,
        transcript_path=attempt.transcript_path,
        outcome_path=attempt.outcome_path,
        summary=message,
    )
    return _with_committed_outcome(
        replace(
            state,
            active_attempt=None,
            stop=StopState(reason=reason, phase_id=attempt.phase.id, message=message),
            last_run=last_run,
        )
    )


def _record_attempt_failure(
    state_path: Path,
    state: RunnerState,
    reason: StopReason,
    message: str,
) -> RunnerOutcome:
    updated = _attempt_failure_state(state, reason, message)
    try:
        _write_transition_outcome(state, updated)
    except OSError as write_error:
        return RunnerOutcome(
            EXIT_AGENT_FAILED,
            f"{reason.value}: {message}; could not write attempt outcome: {write_error}",
            state,
        )
    try:
        write_state(state_path, updated)
    except OSError as write_error:
        return RunnerOutcome(
            EXIT_AGENT_FAILED,
            f"{reason.value}: {message}; could not record failure: {write_error}",
            state,
        )
    return RunnerOutcome(EXIT_AGENT_FAILED, f"{reason.value}: {message}", updated)


def _resolve_abandoned_attempt(
    state: RunnerState,
    attempt: ActiveAttempt,
) -> RunnerState:
    message = "previous handler ended without recording a terminal outcome; result is unknown"
    return replace(
        state,
        active_attempt=None,
        stop=None,
        last_run=LastRun(
            run_id=attempt.id,
            phase_id=attempt.phase.id,
            status=AttemptStatus.INTERRUPTED,
            started_at=attempt.started_at,
            finished_at=format_utc_timestamp(),
            exit_code=EXIT_AGENT_FAILED,
            execution_workspace=attempt.execution_workspace,
            prompt_path=attempt.prompt_path,
            transcript_path=attempt.transcript_path,
            outcome_path=None,
            summary=message,
        ),
    )


def _with_committed_outcome(state: RunnerState) -> RunnerState:
    last_run = state.last_run
    if last_run is None or last_run.outcome_path is None:
        raise ValueError("committed outcome transition requires a linked last run")
    reference = OutcomeRef(
        attempt_id=last_run.run_id,
        phase_id=last_run.phase_id,
        status=last_run.status,
        path=last_run.outcome_path,
    )
    return replace(state, committed_outcomes=(*state.committed_outcomes, reference))


def _write_transition_outcome(previous: RunnerState, updated: RunnerState) -> None:
    attempt = previous.active_attempt
    last_run = updated.last_run
    if attempt is None or last_run is None or last_run.run_id != attempt.id:
        raise ValueError("outcome transition does not match the active attempt")
    if last_run.outcome_path is None:
        raise ValueError("outcome transition does not have an outcome path")
    write_outcome(
        Path(last_run.outcome_path),
        OutcomeRecord(
            attempt_id=attempt.id,
            plan=attempt.snapshot,
            phase=attempt.phase,
            status=last_run.status,
            summary=last_run.summary,
            started_at=last_run.started_at,
            finished_at=last_run.finished_at,
            execution_workspace=last_run.execution_workspace,
            artifacts=OutcomeArtifacts(
                prompt_path=last_run.prompt_path,
                transcript_path=last_run.transcript_path,
            ),
        ),
    )


def _failure_message(reason: StopReason, process_exit_code: int) -> str:
    if reason is StopReason.AGENT_FAILED:
        return f"agent command exited with code {process_exit_code}"
    if reason is StopReason.MISSING_MARKER:
        return "agent output did not contain a terminal marker"
    if reason is StopReason.MULTIPLE_MARKERS:
        return "agent output contained multiple terminal markers"
    if reason is StopReason.INVALID_MARKER:
        return "agent output contained an invalid terminal marker"
    return reason.value


def _read_stream(
    stream_name: str,
    stream: TextIO,
    output_queue: Queue[_OutputQueueItem],
) -> None:
    try:
        while True:
            chunk = stream.readline()
            if chunk == "":
                break
            output_queue.put(_StreamItem(stream_name=stream_name, text=chunk))
    except BaseException as error:
        output_queue.put(_StreamFailure(stream_name=stream_name, error=error))


def _write_stdin(stream: TextIO, prompt_text: str) -> None:
    try:
        stream.write(prompt_text)
    except (BrokenPipeError, OSError, ValueError):
        return
    finally:
        with suppress(BrokenPipeError, OSError, ValueError):
            stream.close()


def _drain_output_queue(
    output_queue: Queue[_OutputQueueItem],
    transcript: TextIO,
    combined_parts: list[str],
    stream_parts: dict[str, list[str]],
    stdout: TextIO | None,
    stderr: TextIO | None,
    display_filters: dict[str, TerminalMarkerFilter],
) -> None:
    while True:
        try:
            item = output_queue.get_nowait()
        except Empty:
            return
        _write_stream_item(
            item,
            transcript,
            combined_parts,
            stream_parts,
            stdout,
            stderr,
            display_filters,
        )


def _write_stream_item(
    item: _OutputQueueItem,
    transcript: TextIO,
    combined_parts: list[str],
    stream_parts: dict[str, list[str]],
    stdout: TextIO | None,
    stderr: TextIO | None,
    display_filters: dict[str, TerminalMarkerFilter],
) -> None:
    if isinstance(item, _StreamFailure):
        raise OSError(f"failed reading agent {item.stream_name}: {item.error}") from item.error
    combined_parts.append(item.text)
    stream_parts[item.stream_name].append(item.text)
    target = stdout if item.stream_name == "stdout" else stderr
    if target is not None:
        display_text = display_filters[item.stream_name].filter(item.text)
        if display_text:
            target.write(display_text)
            target.flush()
    transcript.write(item.text)
    transcript.flush()


def _finish_display_filters(
    display_filters: dict[str, TerminalMarkerFilter],
    stdout: TextIO | None,
    stderr: TextIO | None,
) -> None:
    for stream_name, target in (("stdout", stdout), ("stderr", stderr)):
        if target is None:
            continue
        display_text = display_filters[stream_name].finish()
        if display_text:
            target.write(display_text)
            target.flush()


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


def _terminate_process_group(process: subprocess.Popen[str], process_group_id: int) -> None:
    if process_group_id == os.getpgrp():
        raise RuntimeError("refusing to signal the handler's own process group")

    _signal_process_group(process_group_id, signal.SIGTERM)
    deadline = time.monotonic() + _PROCESS_GROUP_GRACE_SECONDS
    while _process_group_exists(process_group_id) and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.01)

    if _process_group_exists(process_group_id):
        _signal_process_group(process_group_id, signal.SIGKILL)

    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_PROCESS_GROUP_GRACE_SECONDS)


def _signal_process_group(process_group_id: int, signal_number: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process_group_id, signal_number)


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_process_identity(process_id: int) -> ProcessIdentity | None:
    """Read a Linux identity that does not confuse a reused numeric PID."""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        stat = Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8")
        stat_fields = stat.rsplit(")", 1)[1].split()
        start_time_ticks = int(stat_fields[19])
        process_group_id = os.getpgid(process_id)
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None
    return ProcessIdentity(
        pid=process_id,
        process_group_id=process_group_id,
        boot_id=boot_id,
        start_time_ticks=start_time_ticks,
    )


def _active_worker_is_alive(attempt: ActiveAttempt) -> bool:
    identity = attempt.process
    if identity is None:
        return False
    current = _read_process_identity(identity.pid)
    if current != identity:
        return False
    try:
        stat = Path(f"/proc/{identity.pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    process_state = stat.rsplit(")", 1)[1].split()[0]
    return process_state not in {"Z", "X"}


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
