"""Tests for durable runner state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from ai_session_handler.config import ConfigError, default_state_path, plan_key
from ai_session_handler.phases import Phase, parse_phases, read_plan_snapshot
from ai_session_handler.state import (
    AcceptedPlanChangeError,
    ActiveAttempt,
    ActiveAttemptError,
    ActiveAttemptStatus,
    AttemptStatus,
    LastRun,
    PhaseRef,
    PlanHashMismatchError,
    PlanPathMismatchError,
    PlanRecord,
    ProcessIdentity,
    RunnerState,
    SnapshotIdentity,
    StateError,
    StoppedStateError,
    StopReason,
    StopState,
    accept_plan,
    compute_plan_hash,
    ensure_plan_hash_matches,
    read_state,
    select_next_phase,
    with_current_phase,
    write_state,
)

ACCEPTED_AT = datetime(2026, 7, 5, 12, 1, 2, tzinfo=UTC)


def test_missing_state_reads_as_new_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"

    state = read_state(state_path)

    assert state == RunnerState()


def test_state_round_trips_through_stable_json(tmp_path: Path) -> None:
    state_path = tmp_path / ".ai-session-handler" / "plan.json"
    run_id = "20260705T120102Z-phase-2"
    prompt_path = state_path.parent / "prompts" / f"{run_id}.txt"
    transcript_path = state_path.parent / "transcripts" / f"{run_id}.txt"
    state = RunnerState(
        plan=PlanRecord(
            path=str(tmp_path / "docs/plans/example.md"),
            sha256="a" * 64,
            accepted_at="2026-07-05T12:01:02Z",
        ),
        completed_phase_ids=("phase-1",),
        current_phase=PhaseRef(id="phase-2", title="State Store"),
        stop=StopState(
            reason=StopReason.NEEDS_CLARIFICATION,
            phase_id="phase-2",
            clarification_request="Which state file path should be used?",
        ),
        last_run=LastRun(
            run_id=run_id,
            phase_id="phase-2",
            status=AttemptStatus.NEEDS_CLARIFICATION,
            started_at="2026-07-05T12:01:02Z",
            finished_at="2026-07-05T12:02:03Z",
            exit_code=3,
            execution_workspace=str(tmp_path),
            prompt_path=str(prompt_path),
            transcript_path=str(transcript_path),
            summary="Asked for clarification.",
        ),
    )

    write_state(state_path, state)

    assert read_state(state_path) == state
    state_text = state_path.read_text(encoding="utf-8")
    raw_state: object = json.loads(state_text)
    assert isinstance(raw_state, dict)
    assert state_text.endswith("\n")
    assert "schema_version" not in raw_state


def test_active_attempt_round_trips_with_process_identity(tmp_path: Path) -> None:
    state_path = tmp_path / ".ai-session-handler" / "plan.json"
    plan_path = tmp_path / "plan.md"
    run_id = "20260705T120102Z-phase-2-attempt"
    phase = PhaseRef(id="phase-2", title="State Store")
    attempt = ActiveAttempt(
        id=run_id,
        status=ActiveAttemptStatus.RUNNING,
        phase=phase,
        snapshot=SnapshotIdentity(path=str(plan_path), sha256="a" * 64),
        execution_workspace=str(tmp_path),
        started_at="2026-07-05T12:01:02Z",
        prompt_path=str(state_path.parent / "prompts" / f"{run_id}.txt"),
        transcript_path=str(state_path.parent / "transcripts" / f"{run_id}.txt"),
        process=ProcessIdentity(
            pid=123,
            process_group_id=123,
            boot_id="boot-id",
            start_time_ticks=456,
        ),
    )
    state = RunnerState(
        plan=PlanRecord(
            path=str(plan_path),
            sha256="a" * 64,
            accepted_at="2026-07-05T12:00:00Z",
        ),
        current_phase=phase,
        active_attempt=attempt,
    )

    write_state(state_path, state)

    assert read_state(state_path) == state


def test_active_attempt_snapshot_must_match_accepted_plan(tmp_path: Path) -> None:
    state_path = tmp_path / ".ai-session-handler" / "state.json"
    phase = PhaseRef(id="phase-1", title="One")
    run_id = "attempt-1"
    state = RunnerState(
        plan=PlanRecord(
            path=str(tmp_path / "plan.md"),
            sha256="a" * 64,
            accepted_at="2026-07-05T12:00:00Z",
        ),
        current_phase=phase,
        active_attempt=ActiveAttempt(
            id=run_id,
            status=ActiveAttemptStatus.PREPARED,
            phase=phase,
            snapshot=SnapshotIdentity(path=str(tmp_path / "plan.md"), sha256="b" * 64),
            execution_workspace=str(tmp_path),
            started_at="2026-07-05T12:01:02Z",
            prompt_path=str(state_path.parent / "prompts" / f"{run_id}.txt"),
            transcript_path=str(state_path.parent / "transcripts" / f"{run_id}.txt"),
        ),
    )

    with pytest.raises(
        StateError,
        match=r"active_attempt\.snapshot\.sha256 must match plan\.sha256",
    ):
        write_state(state_path, state)


def test_state_rejects_unexpected_top_level_key(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        '{"plan": null, "completed_phase_ids": [], "current_phase": null, '
        '"active_attempt": null, "stop": null, "last_run": null, "obsolete": 1}\n',
        encoding="utf-8",
    )

    with pytest.raises(StateError, match=r"state\.json: unexpected key obsolete"):
        read_state(state_path)


def test_state_requires_explicit_active_attempt_key(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        '{"plan": null, "completed_phase_ids": [], '
        '"current_phase": null, "stop": null, "last_run": null}\n',
        encoding="utf-8",
    )

    with pytest.raises(StateError, match="missing required key active_attempt"):
        read_state(state_path)


def test_state_rejects_unexpected_nested_key(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        '{"plan": {"path": "/workspace/plan.md", "sha256": "'
        + "a" * 64
        + '", "accepted_at": "2026-07-05T12:00:00Z", "obsolete": true}, '
        '"completed_phase_ids": [], "current_phase": null, "active_attempt": null, '
        '"stop": null, "last_run": null}\n',
        encoding="utf-8",
    )

    with pytest.raises(StateError, match=r"state\.json: unexpected key plan\.obsolete"):
        read_state(state_path)


def test_compute_plan_hash_reads_plan_bytes(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_bytes(b"## Phase 1: One\nBody\n")

    assert compute_plan_hash(plan_path) == sha256(b"## Phase 1: One\nBody\n").hexdigest()


def test_plan_key_uses_canonical_workspace_relative_path(tmp_path: Path) -> None:
    plan_path = tmp_path / "docs" / "plans" / "example.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.touch()

    canonical_key = plan_key(tmp_path, plan_path)
    alias_key = plan_key(tmp_path / ".", tmp_path / "docs" / ".." / "docs" / "plans" / "example.md")

    assert canonical_key == alias_key
    assert len(canonical_key) == 64


def test_same_stem_plans_have_distinct_state_paths(tmp_path: Path) -> None:
    first = tmp_path / "docs" / "one" / "plan.md"
    second = tmp_path / "docs" / "two" / "plan.md"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("same", encoding="utf-8")
    second.write_text("same", encoding="utf-8")

    assert default_state_path(tmp_path, first) != default_state_path(tmp_path, second)


def test_plan_key_rejects_plan_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan_path = tmp_path / "outside.md"
    plan_path.touch()

    with pytest.raises(ConfigError, match="plan is outside workspace"):
        plan_key(workspace, plan_path)


def test_new_state_accepts_current_plan_hash(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path, _plan("One", "Body\n"))
    snapshot = read_plan_snapshot(plan_path)

    state = ensure_plan_hash_matches(
        RunnerState(),
        snapshot,
        accepted_at=ACCEPTED_AT,
    )

    assert state.plan == PlanRecord(
        path=str(plan_path),
        sha256=compute_plan_hash(plan_path),
        accepted_at="2026-07-05T12:01:02Z",
    )


def test_completed_phase_selection_returns_first_incomplete_phase() -> None:
    phases = _phases()
    state = RunnerState(completed_phase_ids=("phase-1",))

    assert select_next_phase(state, phases) == phases[1]


def test_completed_phase_selection_returns_none_when_all_complete() -> None:
    phases = _phases()
    state = RunnerState(completed_phase_ids=("phase-1", "phase-2"))

    assert select_next_phase(state, phases) is None


def test_plan_hash_mismatch_is_rejected_by_default(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path, _plan("One", "Original\n"))
    snapshot = read_plan_snapshot(plan_path)
    state = ensure_plan_hash_matches(
        RunnerState(),
        snapshot,
        accepted_at=ACCEPTED_AT,
    )
    plan_path.write_text(_plan("One", "Changed\n"), encoding="utf-8")

    with pytest.raises(PlanHashMismatchError):
        ensure_plan_hash_matches(state, read_plan_snapshot(plan_path))


def test_plan_path_mismatch_is_rejected_even_when_content_hash_matches(tmp_path: Path) -> None:
    first_path = tmp_path / "first.md"
    second_path = tmp_path / "second.md"
    plan_text = _plan("One", "Same content\n")
    first_path.write_text(plan_text, encoding="utf-8")
    second_path.write_text(plan_text, encoding="utf-8")
    state = ensure_plan_hash_matches(
        RunnerState(),
        read_plan_snapshot(first_path),
        accepted_at=ACCEPTED_AT,
    )

    with pytest.raises(PlanPathMismatchError, match="plan path mismatch"):
        ensure_plan_hash_matches(
            state,
            read_plan_snapshot(second_path),
            accept_plan_change=True,
        )


def test_retry_stopped_is_required_for_stopped_state() -> None:
    phases = _phases()
    state = RunnerState(
        current_phase=PhaseRef(id="phase-2", title="Two"),
        stop=StopState(reason=StopReason.BLOCKED, phase_id="phase-2", message="Blocked."),
    )

    with pytest.raises(StoppedStateError):
        select_next_phase(state, phases)

    assert select_next_phase(state, phases, retry_stopped=True) == phases[1]


def test_retry_stopped_is_required_for_active_attempt(tmp_path: Path) -> None:
    phases = _phases()
    phase = PhaseRef(id="phase-2", title="Two")
    run_id = "attempt-2"
    state = RunnerState(
        current_phase=phase,
        active_attempt=ActiveAttempt(
            id=run_id,
            status=ActiveAttemptStatus.PREPARED,
            phase=phase,
            snapshot=SnapshotIdentity(path=str(tmp_path / "plan.md"), sha256="a" * 64),
            execution_workspace=str(tmp_path),
            started_at="2026-07-05T12:01:02Z",
            prompt_path=str(tmp_path / "prompts" / f"{run_id}.txt"),
            transcript_path=str(tmp_path / "transcripts" / f"{run_id}.txt"),
        ),
    )

    with pytest.raises(ActiveAttemptError):
        select_next_phase(state, phases)

    assert select_next_phase(state, phases, retry_stopped=True) == phases[1]


def test_accept_plan_change_updates_hash_when_completed_phase_ids_still_exist(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path, _plan("One", "Original\n") + _plan("Two", "", number=2))
    snapshot = read_plan_snapshot(plan_path)
    state = ensure_plan_hash_matches(
        RunnerState(completed_phase_ids=("phase-1",)),
        snapshot,
        accepted_at=ACCEPTED_AT,
    )
    plan_path.write_text(
        _plan("One Renamed", "Changed\n") + _plan("Two", "", number=2),
        encoding="utf-8",
    )
    changed_snapshot = read_plan_snapshot(plan_path)

    accepted = ensure_plan_hash_matches(
        state,
        changed_snapshot,
        accept_plan_change=True,
        accepted_at=datetime(2026, 7, 5, 13, 0, 0, tzinfo=UTC),
    )

    assert accepted.plan == PlanRecord(
        path=str(plan_path),
        sha256=compute_plan_hash(plan_path),
        accepted_at="2026-07-05T13:00:00Z",
    )
    assert accepted.completed_phase_ids == ("phase-1",)


def test_accept_plan_change_rejects_missing_completed_phase_ids(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path, _plan("One", "Original\n") + _plan("Two", "", number=2))
    snapshot = read_plan_snapshot(plan_path)
    state = accept_plan(
        RunnerState(completed_phase_ids=("phase-1",)),
        snapshot,
        accepted_at=ACCEPTED_AT,
    )
    plan_path.write_text(_plan("Two", "Changed\n", number=2), encoding="utf-8")
    changed_snapshot = read_plan_snapshot(plan_path)

    with pytest.raises(AcceptedPlanChangeError):
        ensure_plan_hash_matches(
            state,
            changed_snapshot,
            accept_plan_change=True,
            accepted_at=ACCEPTED_AT,
        )


def test_with_current_phase_updates_phase_reference() -> None:
    phase = _phases()[0]

    state = with_current_phase(RunnerState(), phase)

    assert state.current_phase == PhaseRef(id="phase-1", title="One")
    assert with_current_phase(state, None).current_phase is None


def _write_plan(tmp_path: Path, text: str) -> Path:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(text, encoding="utf-8")
    return plan_path


def _phases() -> list[Phase]:
    return parse_phases(
        _plan("One", "First\n") + _plan("Two", "Second\n", number=2),
        source="plan.md",
    )


def _plan(title: str, body: str, *, number: int = 1) -> str:
    return f"## Phase {number}: {title}\n### Workspace\n\n.\n\n### Goal\n\n{body}"
