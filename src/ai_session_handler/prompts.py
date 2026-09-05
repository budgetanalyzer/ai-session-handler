"""Worker prompt rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ai_session_handler.artifacts import write_text_exclusively
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

    plan_workspace_path: Path
    execution_workspace_path: Path
    plan_path: Path
    state_path: Path
    phase: Phase
    state: RunnerState
    run_id: str
    transcript_path: Path
    plan_preamble: str


def render_worker_prompt(context: PromptContext) -> str:
    """Render the protocol prompt for one selected phase."""
    global_intent_section = _delimited_section(
        "GLOBAL PLAN INTENT",
        context.plan_preamble,
    )
    phase_body_section = _selected_phase_body_section(context.phase.body)
    state_summary = summarize_previous_state(context.state)
    handoff_context = summarize_handoff_context(context.state)

    return (
        "AI SESSION HANDLER WORKER PROTOCOL\n"
        "\n"
        "CONTEXT\n"
        f"- plan_workspace_path: {context.plan_workspace_path}\n"
        f"- execution_workspace_path: {context.execution_workspace_path}\n"
        f"- plan_path: {context.plan_path}\n"
        f"- state_path: {context.state_path}\n"
        f"- run_id: {context.run_id}\n"
        f"- transcript_path: {context.transcript_path}\n"
        f"- selected_phase_id: {context.phase.id}\n"
        f"- selected_phase_title: {context.phase.title}\n"
        "\n"
        "INSTRUCTIONS\n"
        "- Read repository instructions first from execution_workspace_path, beginning with "
        "the AGENTS.md at its root.\n"
        "- Inspect current repository state before editing.\n"
        "- Read the global plan intent, durable handoff context, and any prior outcome records "
        "relevant to this phase before making decisions.\n"
        f"- Implement exactly selected phase {context.phase.id}: {context.phase.title}.\n"
        "- Do not proceed to later phases.\n"
        "- Restrict execution work to execution_workspace_path and the selected phase. Do not "
        "perform work in another repository.\n"
        "- Follow the selected phase plan unless repository reality contradicts it.\n"
        "- Run the validation commands listed in the selected phase.\n"
        "- Do not run git commit, push, checkout, reset, clean, stash, branch creation, "
        "or automatic worktree operations.\n"
        "- Update only files required for the selected phase.\n"
        "- Treat state_path and outcome records as runner-owned and read-only. Do not create, "
        "edit, replace, or delete them; report the phase outcome only through the terminal "
        "marker.\n"
        "- Store durable design context in ordinary repository documentation when this phase "
        "requires later phases to rely on it.\n"
        "- Do not make design-changing guesses. If implementation requires an unplanned "
        "product, architecture, schema, API, or workflow decision, stop with "
        "<phase-needs-clarification>.\n"
        "- Treat user clarification as a first-class stop state, not as failure.\n"
        "- End one output stream with exactly one terminal marker from the grammar below. The "
        "opening tag must begin at a line boundary, the result must be nonempty, and only "
        "whitespace may follow the closing tag on that stream.\n"
        "- Do not emit recognized terminal tags anywhere else, including examples, quoted text, "
        "fenced blocks, progress, or diagnostics. Do not emit a result on both stdout and stderr.\n"
        "\n"
        "FAILURE MODES\n"
        "- Use <phase-blocked> when the phase cannot be completed without external action.\n"
        "- Use <phase-needs-clarification> when a specific user decision is required.\n"
        "- Use <phase-complete> only after implementation and validation for this phase are "
        "complete.\n"
        "- In every terminal result, summarize changed artifacts, validation commands and "
        "results, decisions, remaining limitations, and relevant handoff references. Keep the "
        "summary concise but sufficient for a fresh worker.\n"
        "\n"
        f"{global_intent_section}\n"
        "\n"
        "PREVIOUS STATE SUMMARY START\n"
        f"{state_summary}\n"
        "PREVIOUS STATE SUMMARY END\n"
        "\n"
        "DURABLE HANDOFF CONTEXT START\n"
        f"{handoff_context}\n"
        "DURABLE HANDOFF CONTEXT END\n"
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
    write_text_exclusively(prompt_path, render_worker_prompt(context))
    return prompt_path


def summarize_previous_state(state: RunnerState) -> str:
    """Render a deterministic summary of durable state for the worker prompt."""
    lines: list[str] = []

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
    lines.append(f"committed_outcome_count: {len(state.committed_outcomes)}")

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

    if state.active_attempt is None:
        lines.append("active_attempt: none")
    else:
        attempt = state.active_attempt
        lines.extend(
            [
                "active_attempt:",
                f"  id: {attempt.id}",
                f"  status: {attempt.status.value}",
                f"  phase_id: {attempt.phase.id}",
                f"  snapshot_sha256: {attempt.snapshot.sha256}",
                f"  execution_workspace: {attempt.execution_workspace}",
                f"  started_at: {attempt.started_at}",
                f"  prompt_path: {attempt.prompt_path}",
                f"  transcript_path: {attempt.transcript_path}",
                f"  outcome_path: {attempt.outcome_path}",
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
                f"  status: {state.last_run.status.value}",
                f"  started_at: {state.last_run.started_at}",
                f"  finished_at: {state.last_run.finished_at}",
                f"  exit_code: {state.last_run.exit_code}",
                f"  execution_workspace: {state.last_run.execution_workspace}",
                f"  prompt_path: {state.last_run.prompt_path}",
                f"  transcript_path: {state.last_run.transcript_path}",
                f"  outcome_path: {_optional_text(state.last_run.outcome_path)}",
                f"  summary: {state.last_run.summary}",
            ]
        )

    return "\n".join(lines)


def summarize_handoff_context(state: RunnerState) -> str:
    """Render the latest summary and a compact index of earlier committed outcomes."""
    lines: list[str] = []
    latest_path: str | None = None
    if state.last_run is None:
        lines.append("latest_relevant_summary: none")
    else:
        latest = state.last_run
        latest_path = latest.outcome_path
        lines.extend(
            [
                "latest_relevant_summary:",
                f"  attempt_id: {latest.run_id}",
                f"  phase_id: {latest.phase_id}",
                f"  status: {latest.status.value}",
                f"  outcome_path: {_optional_text(latest.outcome_path)}",
                "  summary:",
                _indent_text(latest.summary, spaces=4),
            ]
        )

    earlier = [outcome for outcome in state.committed_outcomes if outcome.path != latest_path]
    if not earlier:
        lines.append("earlier_committed_outcomes: none")
    else:
        lines.append("earlier_committed_outcomes:")
        lines.extend(
            f"  - {outcome.phase_id} | {outcome.status.value} | {outcome.path}"
            for outcome in earlier
        )
    return "\n".join(lines)


def _selected_phase_body_section(body: str) -> str:
    return _delimited_section("SELECTED PHASE BODY", body)


def _delimited_section(label: str, text: str) -> str:
    trailing_newline = "" if text.endswith("\n") else "\n"
    return f"{label} START\n{text}{trailing_newline}{label} END"


def _indent_text(text: str, *, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


def _optional_text(value: str | None) -> str:
    return "none" if value is None else value
