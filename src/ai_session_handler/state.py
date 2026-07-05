"""Durable runner state models and persistence."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from ai_session_handler.phases import Phase

SCHEMA_VERSION: Final[int] = 1


class StopReason(StrEnum):
    """Reasons a run can stop before all phases complete."""

    BLOCKED = "blocked"
    NEEDS_CLARIFICATION = "needs-clarification"
    AGENT_FAILED = "agent-failed"
    MISSING_MARKER = "missing-marker"
    MULTIPLE_MARKERS = "multiple-markers"
    TIMEOUT = "timeout"
    STOP_REGEX = "stop-regex"


@dataclass(frozen=True, slots=True)
class PlanRecord:
    """Accepted identity of the source plan file."""

    path: str
    sha256: str
    accepted_at: str


@dataclass(frozen=True, slots=True)
class PhaseRef:
    """Small durable reference to a phase."""

    id: str
    title: str


@dataclass(frozen=True, slots=True)
class StopState:
    """Durable stop information for a phase that needs human attention."""

    reason: StopReason
    phase_id: str
    message: str | None = None
    clarification_request: str | None = None


@dataclass(frozen=True, slots=True)
class LastRun:
    """Summary of the latest agent run recorded in state."""

    run_id: str
    phase_id: str
    status: str
    started_at: str
    finished_at: str
    exit_code: int
    transcript_path: str
    summary: str


@dataclass(frozen=True, slots=True)
class RunnerState:
    """Durable state for one plan file."""

    schema_version: int = SCHEMA_VERSION
    plan: PlanRecord | None = None
    completed_phase_ids: tuple[str, ...] = ()
    current_phase: PhaseRef | None = None
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


class StoppedStateError(StateError):
    """Raised when a stopped state is selected without an explicit retry."""

    def __init__(self, stop: StopState) -> None:
        self.stop = stop
        super().__init__(f"phase {stop.phase_id} is stopped: {stop.reason.value}")


class AcceptedPlanChangeError(StateError):
    """Raised when a changed plan cannot be safely accepted."""


def read_state(path: Path) -> RunnerState:
    """Read runner state from JSON, returning a new empty state when missing."""
    if not path.exists():
        return RunnerState()

    raw: object = json.loads(path.read_text(encoding="utf-8"))
    data = _expect_mapping(raw, source=str(path), key="$")
    schema_version = _int_at(data, "schema_version", source=str(path))
    if schema_version != SCHEMA_VERSION:
        raise StateError(f"{path}: unsupported schema_version {schema_version}")

    return RunnerState(
        schema_version=schema_version,
        plan=_plan_record_from_json(data.get("plan"), source=str(path), key="plan"),
        completed_phase_ids=tuple(
            _string_sequence_at(data, "completed_phase_ids", source=str(path))
        ),
        current_phase=_phase_ref_from_json(
            data.get("current_phase"),
            source=str(path),
            key="current_phase",
        ),
        stop=_stop_state_from_json(data.get("stop"), source=str(path), key="stop"),
        last_run=_last_run_from_json(data.get("last_run"), source=str(path), key="last_run"),
    )


def write_state(path: Path, state: RunnerState) -> None:
    """Write runner state as stable JSON using an atomic replace."""
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
    state: RunnerState,
    plan_path: Path,
    phases: Sequence[Phase],
    *,
    accepted_at: datetime | None = None,
) -> RunnerState:
    """Accept the current plan hash after validating completed phase ids still exist."""
    phase_ids = {phase.id for phase in phases}
    missing_completed = [
        phase_id for phase_id in state.completed_phase_ids if phase_id not in phase_ids
    ]
    if missing_completed:
        missing = ", ".join(missing_completed)
        raise AcceptedPlanChangeError(
            f"{plan_path}: cannot accept plan change; completed phase ids are missing: {missing}"
        )

    return replace(
        state,
        plan=PlanRecord(
            path=str(plan_path),
            sha256=compute_plan_hash(plan_path),
            accepted_at=format_utc_timestamp(accepted_at),
        ),
    )


def ensure_plan_hash_matches(
    state: RunnerState,
    plan_path: Path,
    phases: Sequence[Phase],
    *,
    accept_plan_change: bool = False,
    accepted_at: datetime | None = None,
) -> RunnerState:
    """Return state with an accepted plan hash or raise on an unsafe mismatch."""
    if state.plan is None:
        return accept_plan(state, plan_path, phases, accepted_at=accepted_at)

    actual_sha256 = compute_plan_hash(plan_path)
    if state.plan.sha256 == actual_sha256:
        return state

    if accept_plan_change:
        return accept_plan(state, plan_path, phases, accepted_at=accepted_at)

    raise PlanHashMismatchError(
        expected_sha256=state.plan.sha256,
        actual_sha256=actual_sha256,
        plan_path=plan_path,
    )


def select_next_phase(
    state: RunnerState,
    phases: Sequence[Phase],
    *,
    retry_stopped: bool = False,
) -> Phase | None:
    """Select the first incomplete phase, or the stopped phase when retrying."""
    phases_by_id = {phase.id: phase for phase in phases}

    if state.stop is not None:
        if not retry_stopped:
            raise StoppedStateError(state.stop)
        stopped_phase = phases_by_id.get(state.stop.phase_id)
        if stopped_phase is None:
            raise StateError(f"stopped phase {state.stop.phase_id} does not exist in plan")
        return stopped_phase

    completed = set(state.completed_phase_ids)
    for phase in phases:
        if phase.id not in completed:
            return phase
    return None


def with_current_phase(state: RunnerState, phase: Phase | None) -> RunnerState:
    """Return state with the current phase reference updated."""
    return replace(
        state,
        current_phase=None if phase is None else PhaseRef(id=phase.id, title=phase.title),
    )


def format_utc_timestamp(value: datetime | None = None) -> str:
    """Format a timezone-aware UTC timestamp for durable state."""
    timestamp = datetime.now(UTC) if value is None else value
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise StateError("timestamp must be timezone-aware")
    return timestamp.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _state_to_json(state: RunnerState) -> Mapping[str, object]:
    return {
        "schema_version": state.schema_version,
        "plan": None
        if state.plan is None
        else {
            "path": state.plan.path,
            "sha256": state.plan.sha256,
            "accepted_at": state.plan.accepted_at,
        },
        "completed_phase_ids": list(state.completed_phase_ids),
        "current_phase": None
        if state.current_phase is None
        else {
            "id": state.current_phase.id,
            "title": state.current_phase.title,
        },
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
            "status": state.last_run.status,
            "started_at": state.last_run.started_at,
            "finished_at": state.last_run.finished_at,
            "exit_code": state.last_run.exit_code,
            "transcript_path": state.last_run.transcript_path,
            "summary": state.last_run.summary,
        },
    }


def _plan_record_from_json(value: object, *, source: str, key: str) -> PlanRecord | None:
    if value is None:
        return None
    data = _expect_mapping(value, source=source, key=key)
    return PlanRecord(
        path=_string_at(data, "path", source=source),
        sha256=_string_at(data, "sha256", source=source),
        accepted_at=_string_at(data, "accepted_at", source=source),
    )


def _phase_ref_from_json(value: object, *, source: str, key: str) -> PhaseRef | None:
    if value is None:
        return None
    data = _expect_mapping(value, source=source, key=key)
    return PhaseRef(
        id=_string_at(data, "id", source=source),
        title=_string_at(data, "title", source=source),
    )


def _stop_state_from_json(value: object, *, source: str, key: str) -> StopState | None:
    if value is None:
        return None
    data = _expect_mapping(value, source=source, key=key)
    reason_value = _string_at(data, "reason", source=source)
    try:
        reason = StopReason(reason_value)
    except ValueError as error:
        raise StateError(f"{source}: invalid stop.reason {reason_value!r}") from error

    return StopState(
        reason=reason,
        phase_id=_string_at(data, "phase_id", source=source),
        message=_optional_string_at(data, "message", source=source),
        clarification_request=_optional_string_at(data, "clarification_request", source=source),
    )


def _last_run_from_json(value: object, *, source: str, key: str) -> LastRun | None:
    if value is None:
        return None
    data = _expect_mapping(value, source=source, key=key)
    return LastRun(
        run_id=_string_at(data, "run_id", source=source),
        phase_id=_string_at(data, "phase_id", source=source),
        status=_string_at(data, "status", source=source),
        started_at=_string_at(data, "started_at", source=source),
        finished_at=_string_at(data, "finished_at", source=source),
        exit_code=_int_at(data, "exit_code", source=source),
        transcript_path=_string_at(data, "transcript_path", source=source),
        summary=_string_at(data, "summary", source=source),
    )


def _expect_mapping(value: object, *, source: str, key: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise StateError(f"{source}: expected {key} to be an object")

    result: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise StateError(f"{source}: expected {key} object keys to be strings")
        result[raw_key] = raw_value
    return result


def _string_at(data: Mapping[str, object], key: str, *, source: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise StateError(f"{source}: expected {key} to be a string")
    return value


def _optional_string_at(data: Mapping[str, object], key: str, *, source: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateError(f"{source}: expected {key} to be a string or null")
    return value


def _int_at(data: Mapping[str, object], key: str, *, source: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise StateError(f"{source}: expected {key} to be an integer")
    return value


def _string_sequence_at(data: Mapping[str, object], key: str, *, source: str) -> Sequence[str]:
    value = data.get(key)
    if not isinstance(value, list):
        raise StateError(f"{source}: expected {key} to be a list")

    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise StateError(f"{source}: expected {key}[{index}] to be a string")
        result.append(item)
    return result
