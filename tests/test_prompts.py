"""Tests for worker prompt rendering."""

from __future__ import annotations

from pathlib import Path

from ai_session_handler.phases import Phase
from ai_session_handler.prompts import PromptContext, render_worker_prompt, write_worker_prompt
from ai_session_handler.state import LastRun, PhaseRef, PlanRecord, RunnerState


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
            body="No trailing newline",
            start_line=10,
            end_line=10,
        )
    )

    prompt = render_worker_prompt(context)

    assert "SELECTED PHASE BODY START\nNo trailing newline\nSELECTED PHASE BODY END" in prompt


def test_write_worker_prompt_writes_run_prompt_file(tmp_path: Path) -> None:
    context = _prompt_context()
    generated_dir = tmp_path / ".ai-session-handler"

    prompt_path = write_worker_prompt(generated_dir, context)

    assert prompt_path == generated_dir / "prompts" / "20260705T120102Z-phase-2.txt"
    assert prompt_path.read_text(encoding="utf-8") == render_worker_prompt(context)


def _prompt_context(*, phase: Phase | None = None) -> PromptContext:
    selected_phase = phase or Phase(
        id="phase-2",
        number=2,
        title="Prompt Builder",
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
        current_phase=PhaseRef(id="phase-2", title="Prompt Builder"),
        last_run=LastRun(
            run_id="20260705T115000Z-phase-1",
            phase_id="phase-1",
            status="phase-complete",
            started_at="2026-07-05T11:50:00Z",
            finished_at="2026-07-05T11:55:00Z",
            exit_code=0,
            transcript_path=".ai-session-handler/transcripts/20260705T115000Z-phase-1.txt",
            summary="Completed parser.",
        ),
    )
    return PromptContext(
        workspace_path=Path("/repo"),
        plan_path=Path("/repo/docs/plans/example.md"),
        state_path=Path("/repo/.ai-session-handler/example.json"),
        phase=selected_phase,
        state=state,
        run_id="20260705T120102Z-phase-2",
        transcript_path=Path("/repo/.ai-session-handler/transcripts/20260705T120102Z-phase-2.txt"),
    )
