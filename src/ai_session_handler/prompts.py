"""Worker prompt rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ai_session_handler.phases import Phase
from ai_session_handler.state import RunnerState

MARKER_GRAMMAR: Final[str] = (
    "<phase-complete>summary</phase-complete>\n"
    "<phase-blocked>reason</phase-blocked>\n"
    "<phase-needs-clarification>specific question for user</phase-needs-clarification>"
)


@dataclass(frozen=True, slots=True)
class PromptContext:
    """Inputs required to render one worker prompt."""

    workspace_path: Path
    plan_path: Path
    state_path: Path
    phase: Phase
    state: RunnerState
    run_id: str
    transcript_path: Path


def render_worker_prompt(context: PromptContext) -> str:
    """Render the protocol prompt for one selected phase."""
    phase_body_section = _selected_phase_body_section(context.phase.body)
    state_summary = summarize_previous_state(context.state)

    return (
        "AI SESSION HANDLER WORKER PROTOCOL\n"
        "\n"
        "CONTEXT\n"
        f"- workspace_path: {context.workspace_path}\n"
        f"- plan_path: {context.plan_path}\n"
        f"- state_path: {context.state_path}\n"
        f"- run_id: {context.run_id}\n"
        f"- transcript_path: {context.transcript_path}\n"
        f"- selected_phase_id: {context.phase.id}\n"
        f"- selected_phase_title: {context.phase.title}\n"
        "\n"
        "INSTRUCTIONS\n"
        "- Read repository instructions first, including the nearest AGENTS.md.\n"
        "- Inspect current repository state before editing.\n"
        f"- Implement exactly selected phase {context.phase.id}: {context.phase.title}.\n"
        "- Do not proceed to later phases.\n"
        "- Follow the selected phase plan unless repository reality contradicts it.\n"
        "- Run the validation commands listed in the selected phase.\n"
        "- Do not run git commit, push, checkout, reset, clean, stash, branch creation, "
        "or automatic worktree operations.\n"
        "- Update only files required for the selected phase.\n"
        "- Treat state_path as runner-owned and read-only. Do not create, edit, replace, or "
        "delete the state file; report the phase outcome only through the terminal marker.\n"
        "- Do not make design-changing guesses. If implementation requires an unplanned "
        "product, architecture, schema, API, or workflow decision, stop with "
        "<phase-needs-clarification>.\n"
        "- Treat user clarification as a first-class stop state, not as failure.\n"
        "- End with exactly one terminal marker from the grammar below.\n"
        "- Do not emit more than one terminal marker.\n"
        "\n"
        "FAILURE MODES\n"
        "- Use <phase-blocked> when the phase cannot be completed without external action.\n"
        "- Use <phase-needs-clarification> when a specific user decision is required.\n"
        "- Use <phase-complete> only after implementation and validation for this phase are "
        "complete.\n"
        "\n"
        "PREVIOUS STATE SUMMARY START\n"
        f"{state_summary}\n"
        "PREVIOUS STATE SUMMARY END\n"
        "\n"
        f"{phase_body_section}\n"
        "\n"
        "TERMINAL MARKER GRAMMAR START\n"
        f"{MARKER_GRAMMAR}\n"
        "TERMINAL MARKER GRAMMAR END\n"
    )


def write_worker_prompt(generated_dir: Path, context: PromptContext) -> Path:
    """Write a per-run worker prompt under the generated prompt directory."""
    prompt_dir = generated_dir / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / f"{context.run_id}.txt"
    prompt_path.write_text(render_worker_prompt(context), encoding="utf-8")
    return prompt_path


def summarize_previous_state(state: RunnerState) -> str:
    """Render a deterministic summary of durable state for the worker prompt."""
    lines = [f"schema_version: {state.schema_version}"]

    if state.plan is None:
        lines.append("plan: none")
    else:
        lines.extend(
            [
                "plan:",
                f"  path: {state.plan.path}",
                f"  sha256: {state.plan.sha256}",
                f"  accepted_at: {state.plan.accepted_at}",
            ]
        )

    completed = ", ".join(state.completed_phase_ids) if state.completed_phase_ids else "none"
    lines.append(f"completed_phase_ids: {completed}")

    if state.current_phase is None:
        lines.append("current_phase: none")
    else:
        lines.extend(
            [
                "current_phase:",
                f"  id: {state.current_phase.id}",
                f"  title: {state.current_phase.title}",
            ]
        )

    if state.stop is None:
        lines.append("stop: none")
    else:
        lines.extend(
            [
                "stop:",
                f"  reason: {state.stop.reason.value}",
                f"  phase_id: {state.stop.phase_id}",
                f"  message: {_optional_text(state.stop.message)}",
                f"  clarification_request: {_optional_text(state.stop.clarification_request)}",
            ]
        )

    if state.last_run is None:
        lines.append("last_run: none")
    else:
        lines.extend(
            [
                "last_run:",
                f"  run_id: {state.last_run.run_id}",
                f"  phase_id: {state.last_run.phase_id}",
                f"  status: {state.last_run.status}",
                f"  started_at: {state.last_run.started_at}",
                f"  finished_at: {state.last_run.finished_at}",
                f"  exit_code: {state.last_run.exit_code}",
                f"  transcript_path: {state.last_run.transcript_path}",
                f"  summary: {state.last_run.summary}",
            ]
        )

    return "\n".join(lines)


def _selected_phase_body_section(body: str) -> str:
    trailing_newline = "" if body.endswith("\n") else "\n"
    return f"SELECTED PHASE BODY START\n{body}{trailing_newline}SELECTED PHASE BODY END"


def _optional_text(value: str | None) -> str:
    return "none" if value is None else value
