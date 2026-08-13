"""Tests for markdown plan phase parsing and workspace resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_session_handler.phases import (
    Phase,
    PlanParseError,
    parse_phase_file,
    parse_phases,
    resolve_phase_workspace,
)


def test_parse_normal_phases_preserves_body_workspace_and_lines() -> None:
    markdown = (
        "# Plan\n"
        "\n"
        "Intro text is not part of any phase.\n"
        "\n"
        "## Phase 1: Parse Plans\n"
        "### Workspace\n"
        "\n"
        ".\n"
        "\n"
        "### Goal\n"
        "Goal line\n"
        "\n"
        "### Validation\n"
        "```bash\n"
        "python -m pytest\n"
        "```\n"
        "## Phase 2: State Store\n"
        "### Workspace\n"
        "\n"
        "../state-store\n"
        "\n"
        "### Goal\n"
        "Implement state.\n"
    )

    phases = parse_phases(markdown, source="plan.md")

    assert phases == [
        Phase(
            id="phase-1",
            number=1,
            title="Parse Plans",
            workspace=".",
            workspace_line=8,
            body=(
                "### Workspace\n\n.\n\n### Goal\nGoal line\n\n"
                "### Validation\n```bash\npython -m pytest\n```\n"
            ),
            start_line=5,
            end_line=16,
        ),
        Phase(
            id="phase-2",
            number=2,
            title="State Store",
            workspace="../state-store",
            workspace_line=20,
            body="### Workspace\n\n../state-store\n\n### Goal\nImplement state.\n",
            start_line=17,
            end_line=23,
        ),
    ]


def test_parse_phase_headings_at_any_markdown_level() -> None:
    markdown = (
        "# Phase 1: One\n### Workspace\n.\n### Goal\nOne body\n"
        "### Phase 2: Two\n### Workspace\n.\n### Goal\nTwo body\n"
        "###### Phase 3: Three\n### Workspace\n.\n### Goal\nThree body\n"
    )

    phases = parse_phases(markdown, source="plan.md")

    assert [(phase.id, phase.title, phase.workspace) for phase in phases] == [
        ("phase-1", "One", "."),
        ("phase-2", "Two", "."),
        ("phase-3", "Three", "."),
    ]


def test_phase_body_is_preserved_byte_for_byte() -> None:
    body = "### Workspace\r\n\r\n.\r\n\r\n### Goal\r\nBody without trailing newline"

    phase = parse_phases(f"## Phase 1: One\r\n{body}", source="plan.md")[0]

    assert phase.body == body


def test_parse_phase_file_preserves_crlf_body(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    body = b"### Workspace\r\n\r\n.\r\n\r\n### Goal\r\nBody\r\n"
    plan_path.write_bytes(b"## Phase 1: One\r\n" + body)

    phase = parse_phase_file(plan_path)[0]

    assert phase.body.encode("utf-8") == body


@pytest.mark.parametrize(
    ("body", "message", "line"),
    [
        ("Body\n", "missing required '### Workspace'", 1),
        ("### Workspace\n\n", "empty workspace declaration", 2),
        (
            "### Workspace\n.\n### Goal\nBody\n### Workspace\n.\n",
            "duplicate '### Workspace'",
            6,
        ),
        ("### Workspace\n/tmp/repo\n", "workspace path must be relative", 3),
        ("### Workspace\nC:\\repo\n", "workspace path must be relative", 3),
        ("## Workspace\n.\n", "must use the exact heading", 2),
        ("### Workspace: .\n", "must use the exact heading", 2),
        ("### Workspace\n.\nsecond-line\n", "exactly one relative path line", 4),
    ],
)
def test_invalid_workspace_declarations_are_rejected(
    body: str,
    message: str,
    line: int,
) -> None:
    with pytest.raises(PlanParseError) as error:
        parse_phases(f"## Phase 1: One\n{body}", source="plan.md")

    assert str(error.value).startswith(f"plan.md:{line}: ")
    assert message in str(error.value)


def test_resolve_phase_workspace_requires_directory_and_root_agents_file(tmp_path: Path) -> None:
    plan_workspace = tmp_path / "plan-repo"
    execution_workspace = tmp_path / "service"
    plan_workspace.mkdir()
    execution_workspace.mkdir()
    phase = parse_phases(
        "## Phase 1: Service\n### Workspace\n../service\n",
        source="plan.md",
    )[0]

    with pytest.raises(PlanParseError, match=r"does not contain AGENTS\.md at its root"):
        resolve_phase_workspace(
            phase,
            plan_workspace_path=plan_workspace,
            source="plan.md",
        )

    (execution_workspace / "AGENTS.md").write_text("# Service\n", encoding="utf-8")
    assert (
        resolve_phase_workspace(
            phase,
            plan_workspace_path=plan_workspace,
            source="plan.md",
        )
        == execution_workspace
    )


def test_resolve_phase_workspace_rejects_missing_directory(tmp_path: Path) -> None:
    phase = parse_phases(
        "## Phase 1: Missing\n### Workspace\n../missing\n",
        source="plan.md",
    )[0]

    with pytest.raises(PlanParseError) as error:
        resolve_phase_workspace(
            phase,
            plan_workspace_path=tmp_path / "plan-repo",
            source="plan.md",
        )

    assert "plan.md:3" in str(error.value)
    assert "phase phase-1 workspace '../missing'" in str(error.value)
    assert "not an existing directory" in str(error.value)


def test_duplicate_phase_numbers_are_rejected() -> None:
    with pytest.raises(PlanParseError, match=r"plan.md:4: duplicate phase number 1"):
        parse_phases(
            "## Phase 1: First\n### Workspace\n.\n## Phase 1: Duplicate\n### Workspace\n.\n",
            source="plan.md",
        )


def test_no_phases_is_rejected() -> None:
    with pytest.raises(PlanParseError) as error:
        parse_phases("# Plan\n\nNo explicit phases.\n", source="plan.md")

    assert str(error.value) == (
        "plan.md: expected at least one executable phase heading like '## Phase 1: Title'"
    )


@pytest.mark.parametrize(
    "heading",
    [
        "## phase 1: Lowercase",
        "## Phase 1 Missing Colon",
        "## Phase 1: ",
        " ## Phase 1: Indented",
    ],
)
def test_headings_that_do_not_match_are_ignored(heading: str) -> None:
    markdown = f"{heading}\nIgnored body\n## Phase 2: Real\n### Workspace\n.\n### Goal\nReal body"

    phases = parse_phases(markdown, source="plan.md")

    assert [phase.id for phase in phases] == ["phase-2"]
    assert phases[0].start_line == 3
    assert phases[0].body == "### Workspace\n.\n### Goal\nReal body"


def test_non_monotonic_phase_numbers_are_rejected() -> None:
    with pytest.raises(PlanParseError, match=r"plan.md:4: phase number 2 appears after phase 3"):
        parse_phases(
            "## Phase 3: Later\n### Workspace\n.\n## Phase 2: Earlier\n### Workspace\n.\n",
            source="plan.md",
        )
