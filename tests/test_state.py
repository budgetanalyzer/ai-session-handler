"""Tests for durable runner state."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from ai_session_handler.phases import Phase, parse_phases
from ai_session_handler.state import (
    AcceptedPlanChangeError,
    LastRun,
    PhaseRef,
    PlanHashMismatchError,
    PlanRecord,
    RunnerState,
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
    state = RunnerState(
        plan=PlanRecord(
            path="docs/plans/example.md",
            sha256="abc123",
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
            run_id="20260705T120102Z-phase-2",
            phase_id="phase-2",
            status="needs-clarification",
            started_at="2026-07-05T12:01:02Z",
            finished_at="2026-07-05T12:02:03Z",
            exit_code=3,
            transcript_path=".ai-session-handler/transcripts/run.txt",
            summary="Asked for clarification.",
        ),
    )

    write_state(state_path, state)

    assert read_state(state_path) == state
    assert state_path.read_text(encoding="utf-8").endswith("\n")


def test_compute_plan_hash_reads_plan_bytes(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_bytes(b"## Phase 1: One\nBody\n")

    assert compute_plan_hash(plan_path) == sha256(b"## Phase 1: One\nBody\n").hexdigest()


def test_new_state_accepts_current_plan_hash(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path, "## Phase 1: One\nBody\n")
    phases = parse_phases(plan_path.read_text(encoding="utf-8"), source=str(plan_path))

    state = ensure_plan_hash_matches(
        RunnerState(),
        plan_path,
        phases,
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
    plan_path = _write_plan(tmp_path, "## Phase 1: One\nOriginal\n")
    phases = parse_phases(plan_path.read_text(encoding="utf-8"), source=str(plan_path))
    state = ensure_plan_hash_matches(
        RunnerState(),
        plan_path,
        phases,
        accepted_at=ACCEPTED_AT,
    )
    plan_path.write_text("## Phase 1: One\nChanged\n", encoding="utf-8")

    with pytest.raises(PlanHashMismatchError):
        ensure_plan_hash_matches(state, plan_path, phases)


def test_retry_stopped_is_required_for_stopped_state() -> None:
    phases = _phases()
    state = RunnerState(
        current_phase=PhaseRef(id="phase-2", title="Two"),
        stop=StopState(reason=StopReason.BLOCKED, phase_id="phase-2", message="Blocked."),
    )

    with pytest.raises(StoppedStateError):
        select_next_phase(state, phases)

    assert select_next_phase(state, phases, retry_stopped=True) == phases[1]


def test_accept_plan_change_updates_hash_when_completed_phase_ids_still_exist(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path, "## Phase 1: One\nOriginal\n## Phase 2: Two\n")
    phases = parse_phases(plan_path.read_text(encoding="utf-8"), source=str(plan_path))
    state = ensure_plan_hash_matches(
        RunnerState(completed_phase_ids=("phase-1",)),
        plan_path,
        phases,
        accepted_at=ACCEPTED_AT,
    )
    plan_path.write_text("## Phase 1: One Renamed\nChanged\n## Phase 2: Two\n", encoding="utf-8")
    changed_phases = parse_phases(plan_path.read_text(encoding="utf-8"), source=str(plan_path))

    accepted = ensure_plan_hash_matches(
        state,
        plan_path,
        changed_phases,
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
    plan_path = _write_plan(tmp_path, "## Phase 1: One\nOriginal\n## Phase 2: Two\n")
    phases = parse_phases(plan_path.read_text(encoding="utf-8"), source=str(plan_path))
    state = accept_plan(
        RunnerState(completed_phase_ids=("phase-1",)),
        plan_path,
        phases,
        accepted_at=ACCEPTED_AT,
    )
    plan_path.write_text("## Phase 2: Two\nChanged\n", encoding="utf-8")
    changed_phases = parse_phases(plan_path.read_text(encoding="utf-8"), source=str(plan_path))

    with pytest.raises(AcceptedPlanChangeError):
        ensure_plan_hash_matches(
            state,
            plan_path,
            changed_phases,
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
        "## Phase 1: One\nFirst\n## Phase 2: Two\nSecond\n",
        source="plan.md",
    )
