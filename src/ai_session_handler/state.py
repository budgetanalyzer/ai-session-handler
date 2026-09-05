"""Durable runner state models, validation, and persistence."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from ai_session_handler.phases import Phase, PlanSnapshot


class ActiveAttemptStatus(StrEnum):
    """Lifecycle states for an attempt without a durable outcome."""

    PREPARED = "prepared"
    RUNNING = "running"


class AttemptStatus(StrEnum):
    """Terminal statuses stored for the most recently resolved attempt."""

    PHASE_COMPLETE = "phase-complete"
    BLOCKED = "blocked"
    NEEDS_CLARIFICATION = "needs-clarification"
    AGENT_FAILED = "agent-failed"
    MISSING_MARKER = "missing-marker"
    MULTIPLE_MARKERS = "multiple-markers"
    TIMEOUT = "timeout"
    STOP_REGEX = "stop-regex"
    LAUNCH_FAILED = "launch-failed"
    INTERRUPTED = "interrupted"
    EXECUTION_IO_FAILED = "execution-io-failed"


class StopReason(StrEnum):
    """Reasons a run can stop before all phases complete."""

    BLOCKED = "blocked"
    NEEDS_CLARIFICATION = "needs-clarification"
    AGENT_FAILED = "agent-failed"
    MISSING_MARKER = "missing-marker"
    MULTIPLE_MARKERS = "multiple-markers"
    TIMEOUT = "timeout"
    STOP_REGEX = "stop-regex"
    LAUNCH_FAILED = "launch-failed"
    INTERRUPTED = "interrupted"
    EXECUTION_IO_FAILED = "execution-io-failed"


@dataclass(frozen=True, slots=True)
class PlanRecord:
    """Accepted identity of the source plan file."""

    path: str
    sha256: str
    accepted_at: str


@dataclass(frozen=True, slots=True)
class SnapshotIdentity:
    """Exact plan snapshot used to prepare an attempt."""

    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PhaseRef:
    """Small durable reference to a phase."""

    id: str
    title: str


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Linux process identity strong enough to distinguish PID reuse."""

    pid: int
    process_group_id: int
    boot_id: str
    start_time_ticks: int


@dataclass(frozen=True, slots=True)
class ActiveAttempt:
    """Prepared or running work without a durably recorded terminal outcome."""

    id: str
    status: ActiveAttemptStatus
    phase: PhaseRef
    snapshot: SnapshotIdentity
    execution_workspace: str
    started_at: str
    prompt_path: str
    transcript_path: str
    process: ProcessIdentity | None = None


@dataclass(frozen=True, slots=True)
class StopState:
    """Durable stop information for a phase that needs human attention."""

    reason: StopReason
    phase_id: str
    message: str | None = None
    clarification_request: str | None = None


@dataclass(frozen=True, slots=True)
class LastRun:
    """Summary of the latest terminal attempt recorded in state."""

    run_id: str
    phase_id: str
    status: AttemptStatus
    started_at: str
    finished_at: str
    exit_code: int
    execution_workspace: str
    prompt_path: str
    transcript_path: str
    summary: str


@dataclass(frozen=True, slots=True)
class RunnerState:
    """Durable state for one plan file."""

    plan: PlanRecord | None = None
    completed_phase_ids: tuple[str, ...] = ()
    current_phase: PhaseRef | None = None
    active_attempt: ActiveAttempt | None = None
    stop: StopState | None = None
    last_run: LastRun | None = None


class StateError(ValueError):
    """Raised when runner state is invalid or cannot be advanced safely."""


class PlanHashMismatchError(StateError):
    """Raised when the plan file has changed since state accepted it."""

    def __init__(self, *, expected_sha256: str, actual_sha256: str, plan_path: Path) -> None:
        self.expected_sha256 = expected_sha256
        self.actual_sha256 = actual_sha256
        self.plan_path = plan_path
        super().__init__(
            f"{plan_path}: plan hash mismatch; expected {expected_sha256}, got {actual_sha256}"
        )


class PlanPathMismatchError(StateError):
    """Raised when state belongs to a different canonical plan path."""

    def __init__(self, *, stored_path: str, actual_path: Path) -> None:
        self.stored_path = stored_path
        self.actual_path = actual_path
        super().__init__(
            f"{actual_path}: plan path mismatch; state belongs to {stored_path}. "
            "A renamed or relocated plan requires an explicit history decision."
        )


class StoppedStateError(StateError):
    """Raised when a stopped state is selected without an explicit retry."""

    def __init__(self, stop: StopState) -> None:
        self.stop = stop
        super().__init__(f"phase {stop.phase_id} is stopped: {stop.reason.value}")


class ActiveAttemptError(StateError):
    """Raised when an abandoned active attempt requires inspection and retry."""

    def __init__(self, attempt: ActiveAttempt) -> None:
        self.attempt = attempt
        super().__init__(
            f"phase {attempt.phase.id} has an abandoned {attempt.status.value} attempt "
            f"{attempt.id}; inspect its workspace and artifacts, then use --retry-stopped"
        )


class ActiveWorkerError(StateError):
    """Raised when retry would overlap a positively identified old worker."""

    def __init__(self, attempt: ActiveAttempt) -> None:
        self.attempt = attempt
        assert attempt.process is not None
        super().__init__(
            f"attempt {attempt.id} worker pid {attempt.process.pid} is still alive; "
            "stop it and inspect partial changes before retrying"
        )


class AcceptedPlanChangeError(StateError):
    """Raised when a changed plan cannot be safely accepted."""


def read_state(path: Path) -> RunnerState:
    """Read and validate state JSON, returning a new empty state when missing."""
    if not path.exists():
        return RunnerState()
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StateError(
            f"{path}: invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}"
        ) from error
    data = _expect_mapping(
        raw,
        source=str(path),
        key="$",
        allowed_keys=frozenset(
            {
                "plan",
                "completed_phase_ids",
                "current_phase",
                "active_attempt",
                "stop",
                "last_run",
            }
        ),
    )
    state = RunnerState(
        plan=_plan_record_from_json(
            _value_at(data, "plan", source=str(path)), source=str(path), key="plan"
        ),
        completed_phase_ids=tuple(
            _string_sequence_at(data, "completed_phase_ids", source=str(path))
        ),
        current_phase=_phase_ref_from_json(
            _value_at(data, "current_phase", source=str(path)),
            source=str(path),
            key="current_phase",
        ),
        active_attempt=_active_attempt_from_json(
            _value_at(data, "active_attempt", source=str(path)),
            source=str(path),
            key="active_attempt",
        ),
        stop=_stop_state_from_json(
            _value_at(data, "stop", source=str(path)), source=str(path), key="stop"
        ),
        last_run=_last_run_from_json(
            _value_at(data, "last_run", source=str(path)), source=str(path), key="last_run"
        ),
    )
    _validate_state(state, path)
    return state


def write_state(path: Path, state: RunnerState) -> None:
    """Validate and write runner state as stable JSON using an atomic replace."""
    _validate_state(state, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _state_to_json(state)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            json.dump(payload, temp_file, indent=2)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        Path(temp_name).replace(path)
    except Exception:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
        raise


def compute_plan_hash(path: Path) -> str:
    """Compute the SHA-256 digest of a plan file's bytes."""
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def accept_plan(
    state: RunnerState, snapshot: PlanSnapshot, *, accepted_at: datetime | None = None
) -> RunnerState:
    """Accept the current plan hash after validating completed phase ids still exist."""
    phase_ids = {phase.id for phase in snapshot.phases}
    missing = [phase_id for phase_id in state.completed_phase_ids if phase_id not in phase_ids]
    if missing:
        raise AcceptedPlanChangeError(
            f"{snapshot.path}: cannot accept plan change; completed phase ids are missing: "
            f"{', '.join(missing)}"
        )
    return replace(
        state,
        plan=PlanRecord(
            path=str(snapshot.path),
            sha256=snapshot.sha256,
            accepted_at=format_utc_timestamp(accepted_at),
        ),
    )


def ensure_plan_hash_matches(
    state: RunnerState,
    snapshot: PlanSnapshot,
    *,
    accept_plan_change: bool = False,
    accepted_at: datetime | None = None,
) -> RunnerState:
    """Return state with an accepted plan hash or raise on an unsafe mismatch."""
    if state.plan is None:
        return accept_plan(state, snapshot, accepted_at=accepted_at)
    if state.plan.path != str(snapshot.path):
        raise PlanPathMismatchError(stored_path=state.plan.path, actual_path=snapshot.path)
    if state.plan.sha256 == snapshot.sha256:
        return state
    if accept_plan_change:
        return accept_plan(state, snapshot, accepted_at=accepted_at)
    raise PlanHashMismatchError(
        expected_sha256=state.plan.sha256,
        actual_sha256=snapshot.sha256,
        plan_path=snapshot.path,
    )


def ensure_plan_snapshot_unchanged(snapshot: PlanSnapshot) -> None:
    """Raise when the source bytes no longer match an invocation's snapshot."""
    actual_sha256 = compute_plan_hash(snapshot.path)
    if actual_sha256 != snapshot.sha256:
        raise PlanHashMismatchError(
            expected_sha256=snapshot.sha256,
            actual_sha256=actual_sha256,
            plan_path=snapshot.path,
        )


def select_next_phase(
    state: RunnerState, phases: Sequence[Phase], *, retry_stopped: bool = False
) -> Phase | None:
    """Select the first incomplete phase, or explicitly selected recovery phase."""
    phases_by_id = {phase.id: phase for phase in phases}
    if state.active_attempt is not None:
        if not retry_stopped:
            raise ActiveAttemptError(state.active_attempt)
        phase = phases_by_id.get(state.active_attempt.phase.id)
        if phase is None:
            raise StateError(
                f"active_attempt.phase.id {state.active_attempt.phase.id!r} does not exist in plan"
            )
        return phase
    if state.stop is not None:
        if not retry_stopped:
            raise StoppedStateError(state.stop)
        phase = phases_by_id.get(state.stop.phase_id)
        if phase is None:
            raise StateError(f"stopped phase {state.stop.phase_id} does not exist in plan")
        return phase
    completed = set(state.completed_phase_ids)
    return next((phase for phase in phases if phase.id not in completed), None)


def with_current_phase(state: RunnerState, phase: Phase | None) -> RunnerState:
    """Return state with the current phase reference updated."""
    return replace(
        state, current_phase=None if phase is None else PhaseRef(id=phase.id, title=phase.title)
    )


def format_utc_timestamp(value: datetime | None = None) -> str:
    """Format a timezone-aware UTC timestamp for durable state."""
    timestamp = datetime.now(UTC) if value is None else value
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise StateError("timestamp must be timezone-aware")
    return timestamp.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_state(state: RunnerState, path: Path) -> None:
    source = str(path)
    if len(set(state.completed_phase_ids)) != len(state.completed_phase_ids):
        raise StateError(f"{source}: completed_phase_ids contains duplicates")
    if any(not phase_id for phase_id in state.completed_phase_ids):
        raise StateError(f"{source}: completed_phase_ids cannot contain an empty id")
    if state.current_phase is not None:
        _validate_phase_ref(state.current_phase, source=source, key="current_phase")
    if state.plan is not None:
        _validate_absolute_path(state.plan.path, source=source, key="plan.path")
        _validate_sha256(state.plan.sha256, source=source, key="plan.sha256")
        _validate_timestamp(state.plan.accepted_at, source=source, key="plan.accepted_at")
    if state.active_attempt is not None and state.stop is not None:
        raise StateError(f"{source}: active_attempt and stop cannot both be set")
    if state.active_attempt is not None:
        attempt = state.active_attempt
        if state.plan is None:
            raise StateError(f"{source}: active_attempt requires plan")
        _validate_nonempty(attempt.id, source=source, key="active_attempt.id")
        _validate_phase_ref(attempt.phase, source=source, key="active_attempt.phase")
        if state.current_phase != attempt.phase:
            raise StateError(f"{source}: current_phase must match active_attempt.phase")
        if attempt.phase.id in state.completed_phase_ids:
            raise StateError(f"{source}: active_attempt phase is already completed")
        if attempt.snapshot.path != state.plan.path:
            raise StateError(f"{source}: active_attempt.snapshot.path must match plan.path")
        if attempt.snapshot.sha256 != state.plan.sha256:
            raise StateError(f"{source}: active_attempt.snapshot.sha256 must match plan.sha256")
        _validate_sha256(
            attempt.snapshot.sha256, source=source, key="active_attempt.snapshot.sha256"
        )
        _validate_absolute_path(
            attempt.execution_workspace,
            source=source,
            key="active_attempt.execution_workspace",
        )
        _validate_timestamp(attempt.started_at, source=source, key="active_attempt.started_at")
        _validate_artifact_path(
            attempt.prompt_path,
            expected_parent=path.parent / "prompts",
            expected_name=f"{attempt.id}.txt",
            source=source,
            key="active_attempt.prompt_path",
        )
        _validate_artifact_path(
            attempt.transcript_path,
            expected_parent=path.parent / "transcripts",
            expected_name=f"{attempt.id}.txt",
            source=source,
            key="active_attempt.transcript_path",
        )
        if attempt.status is ActiveAttemptStatus.PREPARED and attempt.process is not None:
            raise StateError(f"{source}: prepared active_attempt.process must be null")
        if attempt.process is not None:
            _validate_nonempty(
                attempt.process.boot_id,
                source=source,
                key="active_attempt.process.boot_id",
            )
    if state.stop is not None:
        _validate_nonempty(state.stop.phase_id, source=source, key="stop.phase_id")
        if state.current_phase is None or state.current_phase.id != state.stop.phase_id:
            raise StateError(f"{source}: current_phase must match stop.phase_id")
        if state.stop.phase_id in state.completed_phase_ids:
            raise StateError(f"{source}: stopped phase is already completed")
        if state.stop.reason is StopReason.NEEDS_CLARIFICATION:
            if not state.stop.clarification_request:
                raise StateError(
                    f"{source}: needs-clarification stop requires clarification_request"
                )
        elif state.stop.clarification_request is not None:
            raise StateError(
                f"{source}: stop.clarification_request requires needs-clarification reason"
            )
    if state.current_phase is not None and state.active_attempt is None and state.stop is None:
        raise StateError(f"{source}: current_phase requires active_attempt or stop")
    if state.last_run is not None:
        last_run = state.last_run
        _validate_nonempty(last_run.run_id, source=source, key="last_run.run_id")
        _validate_nonempty(last_run.phase_id, source=source, key="last_run.phase_id")
        _validate_timestamp(last_run.started_at, source=source, key="last_run.started_at")
        _validate_timestamp(last_run.finished_at, source=source, key="last_run.finished_at")
        _validate_absolute_path(
            last_run.execution_workspace,
            source=source,
            key="last_run.execution_workspace",
        )
        _validate_artifact_path(
            last_run.prompt_path,
            expected_parent=path.parent / "prompts",
            expected_name=f"{last_run.run_id}.txt",
            source=source,
            key="last_run.prompt_path",
        )
        _validate_artifact_path(
            last_run.transcript_path,
            expected_parent=path.parent / "transcripts",
            expected_name=f"{last_run.run_id}.txt",
            source=source,
            key="last_run.transcript_path",
        )
        expected_exit_code = {
            AttemptStatus.PHASE_COMPLETE: 0,
            AttemptStatus.BLOCKED: 2,
            AttemptStatus.NEEDS_CLARIFICATION: 3,
        }.get(last_run.status, 4)
        if last_run.exit_code != expected_exit_code:
            raise StateError(
                f"{source}: last_run.exit_code does not match status {last_run.status.value!r}"
            )
        if (
            last_run.status is AttemptStatus.PHASE_COMPLETE
            and last_run.phase_id not in state.completed_phase_ids
        ):
            raise StateError(f"{source}: completed last_run.phase_id is not in completed_phase_ids")


def _validate_phase_ref(value: PhaseRef, *, source: str, key: str) -> None:
    _validate_nonempty(value.id, source=source, key=f"{key}.id")
    _validate_nonempty(value.title, source=source, key=f"{key}.title")


def _validate_nonempty(value: str, *, source: str, key: str) -> None:
    if not value:
        raise StateError(f"{source}: expected {key} to be nonempty")


def _validate_absolute_path(value: str, *, source: str, key: str) -> None:
    if not Path(value).is_absolute():
        raise StateError(f"{source}: expected {key} to be an absolute path")


def _validate_artifact_path(
    value: str,
    *,
    expected_parent: Path,
    expected_name: str,
    source: str,
    key: str,
) -> None:
    artifact = Path(value)
    if not artifact.is_absolute():
        raise StateError(f"{source}: expected {key} to be an absolute path")
    if artifact.parent != expected_parent.resolve():
        raise StateError(f"{source}: expected {key} to be under {expected_parent.resolve()}")
    if artifact.name != expected_name:
        raise StateError(f"{source}: expected {key} to end with {expected_name}")


def _validate_sha256(value: str, *, source: str, key: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise StateError(f"{source}: expected {key} to be a lowercase SHA-256 digest")


def _validate_timestamp(value: str, *, source: str, key: str) -> None:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StateError(f"{source}: expected {key} to be an ISO 8601 timestamp") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise StateError(f"{source}: expected {key} to include a UTC offset")


def _state_to_json(state: RunnerState) -> Mapping[str, object]:
    return {
        "plan": _plan_record_to_json(state.plan),
        "completed_phase_ids": list(state.completed_phase_ids),
        "current_phase": _phase_ref_to_json(state.current_phase),
        "active_attempt": _active_attempt_to_json(state.active_attempt),
        "stop": None
        if state.stop is None
        else {
            "reason": state.stop.reason.value,
            "phase_id": state.stop.phase_id,
            "message": state.stop.message,
            "clarification_request": state.stop.clarification_request,
        },
        "last_run": None
        if state.last_run is None
        else {
            "run_id": state.last_run.run_id,
            "phase_id": state.last_run.phase_id,
            "status": state.last_run.status.value,
            "started_at": state.last_run.started_at,
            "finished_at": state.last_run.finished_at,
            "exit_code": state.last_run.exit_code,
            "execution_workspace": state.last_run.execution_workspace,
            "prompt_path": state.last_run.prompt_path,
            "transcript_path": state.last_run.transcript_path,
            "summary": state.last_run.summary,
        },
    }


def _plan_record_to_json(value: PlanRecord | None) -> Mapping[str, object] | None:
    return (
        None
        if value is None
        else {"path": value.path, "sha256": value.sha256, "accepted_at": value.accepted_at}
    )


def _phase_ref_to_json(value: PhaseRef | None) -> Mapping[str, object] | None:
    return None if value is None else {"id": value.id, "title": value.title}


def _active_attempt_to_json(value: ActiveAttempt | None) -> Mapping[str, object] | None:
    if value is None:
        return None
    return {
        "id": value.id,
        "status": value.status.value,
        "phase": _phase_ref_to_json(value.phase),
        "snapshot": {"path": value.snapshot.path, "sha256": value.snapshot.sha256},
        "execution_workspace": value.execution_workspace,
        "started_at": value.started_at,
        "prompt_path": value.prompt_path,
        "transcript_path": value.transcript_path,
        "process": None
        if value.process is None
        else {
            "pid": value.process.pid,
            "process_group_id": value.process.process_group_id,
            "boot_id": value.process.boot_id,
            "start_time_ticks": value.process.start_time_ticks,
        },
    }


def _plan_record_from_json(value: object, *, source: str, key: str) -> PlanRecord | None:
    if value is None:
        return None
    data = _expect_mapping(
        value,
        source=source,
        key=key,
        allowed_keys=frozenset({"path", "sha256", "accepted_at"}),
    )
    return PlanRecord(
        path=_string_at(data, "path", source=source),
        sha256=_string_at(data, "sha256", source=source),
        accepted_at=_string_at(data, "accepted_at", source=source),
    )


def _phase_ref_from_json(value: object, *, source: str, key: str) -> PhaseRef | None:
    if value is None:
        return None
    data = _expect_mapping(
        value,
        source=source,
        key=key,
        allowed_keys=frozenset({"id", "title"}),
    )
    return PhaseRef(
        id=_string_at(data, "id", source=source),
        title=_string_at(data, "title", source=source),
    )


def _required_phase_ref_from_json(value: object, *, source: str, key: str) -> PhaseRef:
    result = _phase_ref_from_json(value, source=source, key=key)
    if result is None:
        raise StateError(f"{source}: expected {key} to be an object")
    return result


def _active_attempt_from_json(value: object, *, source: str, key: str) -> ActiveAttempt | None:
    if value is None:
        return None
    data = _expect_mapping(
        value,
        source=source,
        key=key,
        allowed_keys=frozenset(
            {
                "id",
                "status",
                "phase",
                "snapshot",
                "execution_workspace",
                "started_at",
                "prompt_path",
                "transcript_path",
                "process",
            }
        ),
    )
    snapshot = _expect_mapping(
        _value_at(data, "snapshot", source=source),
        source=source,
        key=f"{key}.snapshot",
        allowed_keys=frozenset({"path", "sha256"}),
    )
    return ActiveAttempt(
        id=_string_at(data, "id", source=source),
        status=_enum_at(data, "status", ActiveAttemptStatus, source=source),
        phase=_required_phase_ref_from_json(
            _value_at(data, "phase", source=source), source=source, key=f"{key}.phase"
        ),
        snapshot=SnapshotIdentity(
            path=_string_at(snapshot, "path", source=source),
            sha256=_string_at(snapshot, "sha256", source=source),
        ),
        execution_workspace=_string_at(data, "execution_workspace", source=source),
        started_at=_string_at(data, "started_at", source=source),
        prompt_path=_string_at(data, "prompt_path", source=source),
        transcript_path=_string_at(data, "transcript_path", source=source),
        process=_process_identity_from_json(
            _value_at(data, "process", source=source), source=source, key=f"{key}.process"
        ),
    )


def _process_identity_from_json(value: object, *, source: str, key: str) -> ProcessIdentity | None:
    if value is None:
        return None
    data = _expect_mapping(
        value,
        source=source,
        key=key,
        allowed_keys=frozenset({"pid", "process_group_id", "boot_id", "start_time_ticks"}),
    )
    return ProcessIdentity(
        pid=_positive_int_at(data, "pid", source=source),
        process_group_id=_positive_int_at(data, "process_group_id", source=source),
        boot_id=_string_at(data, "boot_id", source=source),
        start_time_ticks=_positive_int_at(data, "start_time_ticks", source=source),
    )


def _stop_state_from_json(value: object, *, source: str, key: str) -> StopState | None:
    if value is None:
        return None
    data = _expect_mapping(
        value,
        source=source,
        key=key,
        allowed_keys=frozenset({"reason", "phase_id", "message", "clarification_request"}),
    )
    return StopState(
        reason=_enum_at(data, "reason", StopReason, source=source),
        phase_id=_string_at(data, "phase_id", source=source),
        message=_optional_string_at(data, "message", source=source),
        clarification_request=_optional_string_at(data, "clarification_request", source=source),
    )


def _last_run_from_json(value: object, *, source: str, key: str) -> LastRun | None:
    if value is None:
        return None
    data = _expect_mapping(
        value,
        source=source,
        key=key,
        allowed_keys=frozenset(
            {
                "run_id",
                "phase_id",
                "status",
                "started_at",
                "finished_at",
                "exit_code",
                "execution_workspace",
                "prompt_path",
                "transcript_path",
                "summary",
            }
        ),
    )
    return LastRun(
        run_id=_string_at(data, "run_id", source=source),
        phase_id=_string_at(data, "phase_id", source=source),
        status=_enum_at(data, "status", AttemptStatus, source=source),
        started_at=_string_at(data, "started_at", source=source),
        finished_at=_string_at(data, "finished_at", source=source),
        exit_code=_int_at(data, "exit_code", source=source),
        execution_workspace=_string_at(data, "execution_workspace", source=source),
        prompt_path=_string_at(data, "prompt_path", source=source),
        transcript_path=_string_at(data, "transcript_path", source=source),
        summary=_string_at(data, "summary", source=source),
    )


def _expect_mapping(
    value: object,
    *,
    source: str,
    key: str,
    allowed_keys: frozenset[str],
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise StateError(f"{source}: expected {key} to be an object")
    result: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise StateError(f"{source}: expected {key} object keys to be strings")
        result[raw_key] = raw_value
    unexpected_keys = sorted(result.keys() - allowed_keys)
    if unexpected_keys:
        unexpected_key = unexpected_keys[0]
        qualified_key = unexpected_key if key == "$" else f"{key}.{unexpected_key}"
        raise StateError(f"{source}: unexpected key {qualified_key}")
    return result


def _string_at(data: Mapping[str, object], key: str, *, source: str) -> str:
    value = _value_at(data, key, source=source)
    if not isinstance(value, str):
        raise StateError(f"{source}: expected {key} to be a string")
    return value


def _optional_string_at(data: Mapping[str, object], key: str, *, source: str) -> str | None:
    value = _value_at(data, key, source=source)
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateError(f"{source}: expected {key} to be a string or null")
    return value


def _int_at(data: Mapping[str, object], key: str, *, source: str) -> int:
    value = _value_at(data, key, source=source)
    if not isinstance(value, int) or isinstance(value, bool):
        raise StateError(f"{source}: expected {key} to be an integer")
    return value


def _positive_int_at(data: Mapping[str, object], key: str, *, source: str) -> int:
    value = _int_at(data, key, source=source)
    if value < 1:
        raise StateError(f"{source}: expected {key} to be a positive integer")
    return value


def _string_sequence_at(data: Mapping[str, object], key: str, *, source: str) -> Sequence[str]:
    value = _value_at(data, key, source=source)
    if not isinstance(value, list):
        raise StateError(f"{source}: expected {key} to be a list")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise StateError(f"{source}: expected {key}[{index}] to be a string")
        result.append(item)
    return result


def _value_at(data: Mapping[str, object], key: str, *, source: str) -> object:
    if key not in data:
        raise StateError(f"{source}: missing required key {key}")
    return data[key]


def _enum_at[EnumType: StrEnum](
    data: Mapping[str, object], key: str, enum_type: type[EnumType], *, source: str
) -> EnumType:
    value = _string_at(data, key, source=source)
    try:
        return enum_type(value)
    except ValueError as error:
        raise StateError(f"{source}: invalid {key} {value!r}") from error
