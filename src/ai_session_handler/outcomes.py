"""Runner-owned terminal outcome records for individual attempts."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ai_session_handler.artifacts import ArtifactExistsError
from ai_session_handler.state import AttemptStatus, PhaseRef, SnapshotIdentity, StateError


@dataclass(frozen=True, slots=True)
class OutcomeArtifacts:
    """Immutable artifact paths associated with an attempt."""

    prompt_path: str
    transcript_path: str


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """One runner-observed terminal result, before or after state commits it."""

    attempt_id: str
    plan: SnapshotIdentity
    phase: PhaseRef
    status: AttemptStatus
    summary: str
    started_at: str
    finished_at: str
    execution_workspace: str
    artifacts: OutcomeArtifacts


class OutcomeError(StateError):
    """Raised when an outcome record does not match the current format."""


def outcome_path(generated_dir: Path, attempt_id: str) -> Path:
    """Return the runner-owned outcome path for an attempt."""
    return generated_dir / "outcomes" / f"{attempt_id}.json"


def write_outcome(path: Path, record: OutcomeRecord) -> None:
    """Create and durably flush one outcome without replacing prior evidence."""
    _validate_outcome(record, source=str(path))
    if path.name != f"{record.attempt_id}.json":
        raise OutcomeError(f"{path}: outcome filename must match attempt id {record.attempt_id!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as outcome_file:
            json.dump(_outcome_to_json(record), outcome_file, indent=2)
            outcome_file.write("\n")
            outcome_file.flush()
            os.fsync(outcome_file.fileno())
    except FileExistsError as error:
        raise ArtifactExistsError(path) from error
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def read_outcome(path: Path) -> OutcomeRecord:
    """Read and validate one outcome record using only the current format."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise OutcomeError(
            f"{path}: invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}"
        ) from error
    data = _expect_mapping(
        raw,
        source=str(path),
        key="$",
        allowed_keys=frozenset(
            {
                "attempt_id",
                "plan",
                "phase",
                "status",
                "summary",
                "started_at",
                "finished_at",
                "execution_workspace",
                "artifacts",
            }
        ),
    )
    plan = _expect_mapping(
        _value_at(data, "plan", source=str(path)),
        source=str(path),
        key="plan",
        allowed_keys=frozenset({"path", "sha256"}),
    )
    phase = _expect_mapping(
        _value_at(data, "phase", source=str(path)),
        source=str(path),
        key="phase",
        allowed_keys=frozenset({"id", "title"}),
    )
    artifacts = _expect_mapping(
        _value_at(data, "artifacts", source=str(path)),
        source=str(path),
        key="artifacts",
        allowed_keys=frozenset({"prompt_path", "transcript_path"}),
    )
    status_text = _string_at(data, "status", source=str(path))
    try:
        status = AttemptStatus(status_text)
    except ValueError as error:
        raise OutcomeError(f"{path}: invalid status {status_text!r}") from error
    record = OutcomeRecord(
        attempt_id=_string_at(data, "attempt_id", source=str(path)),
        plan=SnapshotIdentity(
            path=_string_at(plan, "path", source=str(path)),
            sha256=_string_at(plan, "sha256", source=str(path)),
        ),
        phase=PhaseRef(
            id=_string_at(phase, "id", source=str(path)),
            title=_string_at(phase, "title", source=str(path)),
        ),
        status=status,
        summary=_string_at(data, "summary", source=str(path)),
        started_at=_string_at(data, "started_at", source=str(path)),
        finished_at=_string_at(data, "finished_at", source=str(path)),
        execution_workspace=_string_at(data, "execution_workspace", source=str(path)),
        artifacts=OutcomeArtifacts(
            prompt_path=_string_at(artifacts, "prompt_path", source=str(path)),
            transcript_path=_string_at(artifacts, "transcript_path", source=str(path)),
        ),
    )
    _validate_outcome(record, source=str(path))
    if path.name != f"{record.attempt_id}.json":
        raise OutcomeError(f"{path}: outcome filename does not match attempt_id")
    return record


def _validate_outcome(record: OutcomeRecord, *, source: str) -> None:
    for key, value in (
        ("attempt_id", record.attempt_id),
        ("plan.path", record.plan.path),
        ("plan.sha256", record.plan.sha256),
        ("phase.id", record.phase.id),
        ("phase.title", record.phase.title),
        ("summary", record.summary),
        ("started_at", record.started_at),
        ("finished_at", record.finished_at),
        ("execution_workspace", record.execution_workspace),
        ("artifacts.prompt_path", record.artifacts.prompt_path),
        ("artifacts.transcript_path", record.artifacts.transcript_path),
    ):
        if not value:
            raise OutcomeError(f"{source}: expected {key} to be nonempty")
    if len(record.plan.sha256) != 64 or any(
        character not in "0123456789abcdef" for character in record.plan.sha256
    ):
        raise OutcomeError(f"{source}: expected plan.sha256 to be a lowercase SHA-256 digest")
    for key, value in (
        ("plan.path", record.plan.path),
        ("execution_workspace", record.execution_workspace),
        ("artifacts.prompt_path", record.artifacts.prompt_path),
        ("artifacts.transcript_path", record.artifacts.transcript_path),
    ):
        if not Path(value).is_absolute():
            raise OutcomeError(f"{source}: expected {key} to be an absolute path")
    for key, value in (
        ("started_at", record.started_at),
        ("finished_at", record.finished_at),
    ):
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise OutcomeError(f"{source}: expected {key} to be an ISO 8601 timestamp") from error
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise OutcomeError(f"{source}: expected {key} to include a UTC offset")


def _outcome_to_json(record: OutcomeRecord) -> Mapping[str, object]:
    return {
        "attempt_id": record.attempt_id,
        "plan": {"path": record.plan.path, "sha256": record.plan.sha256},
        "phase": {"id": record.phase.id, "title": record.phase.title},
        "status": record.status.value,
        "summary": record.summary,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "execution_workspace": record.execution_workspace,
        "artifacts": {
            "prompt_path": record.artifacts.prompt_path,
            "transcript_path": record.artifacts.transcript_path,
        },
    }


def _expect_mapping(
    value: object,
    *,
    source: str,
    key: str,
    allowed_keys: frozenset[str],
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise OutcomeError(f"{source}: expected {key} to be an object")
    result: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise OutcomeError(f"{source}: expected {key} object keys to be strings")
        result[raw_key] = raw_value
    unexpected = sorted(result.keys() - allowed_keys)
    if unexpected:
        qualified = unexpected[0] if key == "$" else f"{key}.{unexpected[0]}"
        raise OutcomeError(f"{source}: unexpected key {qualified}")
    return result


def _string_at(data: Mapping[str, object], key: str, *, source: str) -> str:
    value = _value_at(data, key, source=source)
    if not isinstance(value, str):
        raise OutcomeError(f"{source}: expected {key} to be a string")
    return value


def _value_at(data: Mapping[str, object], key: str, *, source: str) -> object:
    if key not in data:
        raise OutcomeError(f"{source}: missing required key {key}")
    return data[key]
