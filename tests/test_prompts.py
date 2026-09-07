"""Tests for worker prompt rendering."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ai_session_handler.artifacts import ArtifactExistsError
from ai_session_handler.phases import Phase
from ai_session_handler.prompts import PromptContext, render_worker_prompt, write_worker_prompt
from ai_session_handler.state import (
    AttemptStatus,
    LastRun,
    OutcomeRef,
    PhaseRef,
    PlanRecord,
    RunnerState,
)


def test_render_worker_prompt_matches_fixture() -> None:
    context = _prompt_context()
    fixture_path = Path(__file__).parent / "fixtures" / "prompts" / "worker_prompt.txt"

    assert render_worker_prompt(context) == fixture_path.read_text(encoding="utf-8")


def test_render_worker_prompt_handles_body_without_trailing_newline() -> None:
    context = _prompt_context(
        phase=Phase(
            id="phase-2",
            number=2,
            title="Prompt Builder",
            workspace="../service",
            workspace_line=12,
            body="No trailing newline",
            start_line=10,
            end_line=10,
        )
    )

    prompt = render_worker_prompt(context)

    assert "SELECTED PHASE BODY START\nNo trailing newline\nSELECTED PHASE BODY END" in prompt


def test_render_worker_prompt_marks_state_as_runner_owned_and_read_only() -> None:
    prompt = render_worker_prompt(_prompt_context())

    assert "Treat state_path and outcome records as runner-owned and read-only" in prompt
    assert "report the phase outcome only through the terminal marker" in prompt


def test_render_worker_prompt_includes_global_intent_and_handoff_requirements() -> None:
    prompt = render_worker_prompt(_prompt_context())

    assert "GLOBAL PLAN INTENT START\n# Example plan\n\nKeep API names stable.\n" in prompt
    assert "GLOBAL PLAN INTENT END" in prompt
    assert "changed artifacts, validation commands and results, decisions" in prompt
    assert "remaining limitations, and relevant handoff references" in prompt
    assert "phase-1 | phase-complete" not in prompt
    assert "status: phase-complete" in prompt
    assert "Completed parser." in prompt


def test_render_worker_prompt_includes_latest_multiline_summary_once() -> None:
    context = _prompt_context()
    assert context.state.last_run is not None
    summary = "Completed parser.\nValidated parser edge cases."
    state = replace(context.state, last_run=replace(context.state.last_run, summary=summary))

    prompt = render_worker_prompt(replace(context, state=state))
    previous_state = prompt.split("PREVIOUS STATE SUMMARY START\n", 1)[1].split(
        "\nPREVIOUS STATE SUMMARY END", 1
    )[0]
    handoff = prompt.split("DURABLE HANDOFF CONTEXT START\n", 1)[1].split(
        "\nDURABLE HANDOFF CONTEXT END", 1
    )[0]

    assert summary not in previous_state
    assert "    Completed parser.\n    Validated parser edge cases." in handoff
    assert prompt.count("Completed parser.") == 1
    assert prompt.count("Validated parser edge cases.") == 1


def test_render_worker_prompt_indexes_earlier_outcomes_by_status_and_path() -> None:
    context = _prompt_context()
    assert context.state.last_run is not None
    latest_path = "/outcomes/latest.json"
    outcomes = (
        _outcome_ref("complete", "phase-1", AttemptStatus.PHASE_COMPLETE),
        _outcome_ref("blocked", "phase-2", AttemptStatus.BLOCKED),
        _outcome_ref("clarification", "phase-2", AttemptStatus.NEEDS_CLARIFICATION),
        _outcome_ref("failed", "phase-2", AttemptStatus.AGENT_FAILED),
        OutcomeRef(
            attempt_id="latest",
            phase_id="phase-2",
            status=AttemptStatus.PHASE_COMPLETE,
            path=latest_path,
        ),
    )
    state = replace(
        context.state,
        committed_outcomes=outcomes,
        last_run=replace(
            context.state.last_run,
            run_id="latest",
            status=AttemptStatus.PHASE_COMPLETE,
            outcome_path=latest_path,
        ),
    )

    prompt = render_worker_prompt(replace(context, state=state))
    earlier = prompt.split("earlier_committed_outcomes:\n", 1)[1].split(
        "\nDURABLE HANDOFF CONTEXT END", 1
    )[0]

    assert "phase-1 | phase-complete | /outcomes/complete.json" in earlier
    assert "phase-2 | blocked | /outcomes/blocked.json" in earlier
    assert "phase-2 | needs-clarification | /outcomes/clarification.json" in earlier
    assert "phase-2 | agent-failed | /outcomes/failed.json" in earlier
    assert latest_path not in earlier


def test_render_worker_prompt_directs_context_reads_from_accepted_snapshot() -> None:
    prompt = render_worker_prompt(_prompt_context())

    assert "exact copies from this invocation's accepted immutable plan snapshot" in prompt
    assert "for ordinary phase startup instead of rereading the complete source plan" in prompt
    assert "Inspect an indexed outcome only when its concrete decision" in prompt
    assert (
        "Treat complete transcripts as diagnostic evidence and do not replay them by default"
        in prompt
    )
    assert "inspect source plan bytes to diagnose a mismatch or repository contradiction" in prompt


def test_render_worker_prompt_requires_strict_terminal_marker_framing() -> None:
    prompt = render_worker_prompt(_prompt_context())

    assert "opening tag must begin at a line boundary" in prompt
    assert "only whitespace may follow the closing tag" in prompt
    assert "including examples, quoted text, fenced blocks, progress, or diagnostics" in prompt
    assert "Do not emit a result on both stdout and stderr" in prompt


def test_write_worker_prompt_writes_run_prompt_file(tmp_path: Path) -> None:
    context = _prompt_context()
    generated_dir = tmp_path / ".ai-session-handler" / "plans" / "plan-key"

    prompt_path = write_worker_prompt(generated_dir, context)

    assert prompt_path == generated_dir / "prompts" / "20260705T120102Z-phase-2.txt"
    assert prompt_path.read_text(encoding="utf-8") == render_worker_prompt(context)


def test_write_worker_prompt_refuses_to_overwrite_existing_attempt(tmp_path: Path) -> None:
    context = _prompt_context()
    generated_dir = tmp_path / ".ai-session-handler" / "plans" / "plan-key"
    prompt_path = write_worker_prompt(generated_dir, context)
    original = prompt_path.read_bytes()

    with pytest.raises(ArtifactExistsError, match="refusing to overwrite"):
        write_worker_prompt(generated_dir, context)

    assert prompt_path.read_bytes() == original


def _prompt_context(*, phase: Phase | None = None) -> PromptContext:
    selected_phase = phase or Phase(
        id="phase-2",
        number=2,
        title="Prompt Builder",
        workspace="../service",
        workspace_line=12,
        body=("### Goal\nBuild prompts.\n\n### Validation\n```bash\npython -m pytest\n```\n"),
        start_line=10,
        end_line=16,
    )
    state = RunnerState(
        plan=PlanRecord(
            path="docs/plans/example.md",
            sha256="abc123",
            accepted_at="2026-07-05T12:00:00Z",
        ),
        completed_phase_ids=("phase-1",),
        committed_outcomes=(
            OutcomeRef(
                attempt_id="20260705T115000Z-phase-1",
                phase_id="phase-1",
                status=AttemptStatus.PHASE_COMPLETE,
                path=(
                    "/plan-repo/.ai-session-handler/plans/plan-key/outcomes/"
                    "20260705T115000Z-phase-1.json"
                ),
            ),
        ),
        current_phase=PhaseRef(id="phase-2", title="Prompt Builder"),
        last_run=LastRun(
            run_id="20260705T115000Z-phase-1",
            phase_id="phase-1",
            status=AttemptStatus.PHASE_COMPLETE,
            started_at="2026-07-05T11:50:00Z",
            finished_at="2026-07-05T11:55:00Z",
            exit_code=0,
            execution_workspace="/service",
            prompt_path=(
                "/plan-repo/.ai-session-handler/plans/plan-key/prompts/20260705T115000Z-phase-1.txt"
            ),
            transcript_path=(
                "/plan-repo/.ai-session-handler/plans/plan-key/transcripts/"
                "20260705T115000Z-phase-1.txt"
            ),
            outcome_path=(
                "/plan-repo/.ai-session-handler/plans/plan-key/outcomes/"
                "20260705T115000Z-phase-1.json"
            ),
            summary="Completed parser.",
        ),
    )
    return PromptContext(
        plan_workspace_path=Path("/plan-repo"),
        execution_workspace_path=Path("/service"),
        plan_path=Path("/plan-repo/docs/plans/example.md"),
        state_path=Path("/plan-repo/.ai-session-handler/plans/plan-key/state.json"),
        phase=selected_phase,
        state=state,
        run_id="20260705T120102Z-phase-2",
        transcript_path=Path(
            "/plan-repo/.ai-session-handler/plans/plan-key/transcripts/20260705T120102Z-phase-2.txt"
        ),
        plan_preamble="# Example plan\n\nKeep API names stable.\n",
    )


def _outcome_ref(name: str, phase_id: str, status: AttemptStatus) -> OutcomeRef:
    return OutcomeRef(
        attempt_id=name,
        phase_id=phase_id,
        status=status,
        path=f"/outcomes/{name}.json",
    )
