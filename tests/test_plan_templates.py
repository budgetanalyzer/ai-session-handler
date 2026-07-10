"""Tests for plan template rendering and creation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_session_handler.phases import PlanParseError, parse_phase_file, parse_phases
from ai_session_handler.plan_templates import (
    INCOMPLETE_PLAN_TEMPLATE_MARKER,
    PlanTemplateError,
    create_plan_template,
    render_plan_template,
)

EXPECTED_PHASE_BODY = (
    "\n"
    "### Goal\n"
    "\n"
    "TODO\n"
    "\n"
    "### Scope\n"
    "\n"
    "TODO\n"
    "\n"
    "### Non-goals\n"
    "\n"
    "TODO\n"
    "\n"
    "### Required context\n"
    "\n"
    "TODO\n"
    "\n"
    "### Implementation notes\n"
    "\n"
    "TODO\n"
    "\n"
    "### Validation\n"
    "\n"
    "TODO\n"
    "\n"
    "### Completion criteria\n"
    "\n"
    "TODO\n"
)


def test_render_plan_template_is_deterministic_and_complete() -> None:
    plan_path = Path("docs/plans/customer_onboarding-rollout.md")

    rendered = render_plan_template(plan_path)

    assert rendered == render_plan_template(plan_path)
    assert rendered.startswith(f"{INCOMPLETE_PLAN_TEMPLATE_MARKER}\n")
    assert "# Customer Onboarding Rollout\n" in rendered
    assert "\n## Phase 1: TODO phase title\n" in rendered
    for section in (
        "### Goal",
        "### Scope",
        "### Non-goals",
        "### Required context",
        "### Implementation notes",
        "### Validation",
        "### Completion criteria",
    ):
        assert section in rendered


def test_create_plan_template_creates_nested_target_only(tmp_path: Path) -> None:
    plan_path = tmp_path / "docs" / "plans" / "example_plan.md"

    create_plan_template(plan_path)

    assert plan_path.read_text(encoding="utf-8") == render_plan_template(plan_path)
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == [
        Path("docs"),
        Path("docs/plans"),
        Path("docs/plans/example_plan.md"),
    ]


def test_create_plan_template_existing_target_fails_without_overwrite(tmp_path: Path) -> None:
    plan_path = tmp_path / "docs" / "plans" / "existing.md"
    plan_path.parent.mkdir(parents=True)
    original_bytes = b"existing bytes\n"
    plan_path.write_bytes(original_bytes)

    with pytest.raises(PlanTemplateError, match=r"plan already exists"):
        create_plan_template(plan_path)

    assert plan_path.read_bytes() == original_bytes


def test_create_plan_template_does_not_create_runner_state(tmp_path: Path) -> None:
    create_plan_template(tmp_path / "docs" / "plans" / "example.md")

    assert not (tmp_path / ".ai-session-handler").exists()


def test_generated_template_is_rejected_by_phase_file_parser(tmp_path: Path) -> None:
    plan_path = tmp_path / "docs" / "plans" / "example.md"
    create_plan_template(plan_path)

    with pytest.raises(PlanParseError) as error:
        parse_phase_file(plan_path)

    message = str(error.value)
    assert f"{plan_path}:1" in message
    assert "replace placeholders and remove the marker" in message


def test_removing_marker_allows_template_phase_body_to_parse_normally() -> None:
    rendered = render_plan_template(Path("example.md"))
    completed = rendered.replace(f"{INCOMPLETE_PLAN_TEMPLATE_MARKER}\n", "", 1)

    phases = parse_phases(completed, source="example.md")

    assert len(phases) == 1
    assert phases[0].id == "phase-1"
    assert phases[0].title == "TODO phase title"
    assert phases[0].body == EXPECTED_PHASE_BODY
