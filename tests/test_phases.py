"""Tests for markdown plan phase parsing."""

from __future__ import annotations

import pytest

from ai_session_handler.phases import Phase, PlanParseError, parse_phases


def test_parse_normal_phases_preserves_body_and_lines() -> None:
    markdown = (
        "# Plan\n"
        "\n"
        "Intro text is not part of any phase.\n"
        "\n"
        "## Phase 1: Parse Plans\n"
        "Goal line\n"
        "\n"
        "### Validation\n"
        "```bash\n"
        "python -m pytest\n"
        "```\n"
        "## Phase 2: State Store\n"
        "Implement state.\n"
    )

    phases = parse_phases(markdown, source="plan.md")

    assert phases == [
        Phase(
            id="phase-1",
            number=1,
            title="Parse Plans",
            body="Goal line\n\n### Validation\n```bash\npython -m pytest\n```\n",
            start_line=5,
            end_line=11,
        ),
        Phase(
            id="phase-2",
            number=2,
            title="State Store",
            body="Implement state.\n",
            start_line=12,
            end_line=13,
        ),
    ]


def test_parse_phase_headings_at_any_markdown_level() -> None:
    markdown = (
        "# Phase 1: One\nOne body\n### Phase 2: Two\nTwo body\n###### Phase 3: Three\nThree body\n"
    )

    phases = parse_phases(markdown, source="plan.md")

    assert [(phase.id, phase.title, phase.body) for phase in phases] == [
        ("phase-1", "One", "One body\n"),
        ("phase-2", "Two", "Two body\n"),
        ("phase-3", "Three", "Three body\n"),
    ]


def test_parse_phase_with_empty_body() -> None:
    phases = parse_phases("## Phase 1: Empty\n## Phase 2: Next\nBody", source="plan.md")

    assert phases[0] == Phase(
        id="phase-1",
        number=1,
        title="Empty",
        body="",
        start_line=1,
        end_line=1,
    )
    assert phases[1].body == "Body"


def test_duplicate_phase_numbers_are_rejected() -> None:
    with pytest.raises(PlanParseError, match=r"plan.md:3: duplicate phase number 1"):
        parse_phases(
            "## Phase 1: First\nBody\n## Phase 1: Duplicate\n",
            source="plan.md",
        )


def test_no_phases_is_rejected() -> None:
    with pytest.raises(PlanParseError) as error:
        parse_phases("# Plan\n\nNo explicit phases.\n", source="plan.md")

    message = str(error.value)
    assert "plan.md: expected at least one executable phase heading like '## Phase 1: Title'" in (
        message
    )
    assert "ai-session-handler create-plan --plan PATH" in message


def test_design_document_headings_are_diagnostic_only() -> None:
    markdown = (
        "# Rollout Design\n"
        "\n"
        "## Stage 1: Discovery\n"
        "\n"
        "### Issue AUTH-101: Token expiry\n"
        "\n"
        "#### Stage 1: Reproduce locally\n"
        "\n"
        "#### Stage 2: Patch validation\n"
        "\n"
        "### Issue AUTH-102: Refresh failure\n"
        "\n"
        "#### Stage 1: Reproduce in staging\n"
        "\n"
        "## Workstream API\n"
        "\n"
        "Implementation order:\n"
        "1. Stage 1: Build API changes\n"
        "2. Issue AUTH-101 before AUTH-102\n"
    )

    with pytest.raises(PlanParseError) as error:
        parse_phases(markdown, source="design.md")

    message = str(error.value)
    assert "design.md: expected at least one executable phase heading like '## Phase 1: Title'" in (
        message
    )
    assert "line 3: Stage 1: Discovery" in message
    assert "line 5: Issue AUTH-101: Token expiry" in message
    assert "line 7: Stage 1: Reproduce locally" in message
    assert "line 11: Issue AUTH-102: Refresh failure" in message
    assert "line 15: Workstream API" in message
    assert "Stage, Workstream, and Issue headings are not executable phases" in message
    assert "will not be interpreted as phases" in message
    assert "Implementation order" not in message


def test_no_phase_diagnostic_headings_are_bounded() -> None:
    markdown = "\n".join(f"## Stage {number}: Work" for number in range(1, 11))

    with pytest.raises(PlanParseError) as error:
        parse_phases(markdown, source="large-design.md")

    message = str(error.value)
    assert "line 8: Stage 8: Work" in message
    assert "line 9: Stage 9: Work" not in message
    assert "and 2 more" in message


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
    markdown = f"{heading}\nIgnored body\n## Phase 2: Real\nReal body"

    phases = parse_phases(markdown, source="plan.md")

    assert [phase.id for phase in phases] == ["phase-2"]
    assert phases[0].start_line == 3
    assert phases[0].body == "Real body"


def test_non_monotonic_phase_numbers_are_rejected() -> None:
    with pytest.raises(PlanParseError, match=r"plan.md:3: phase number 2 appears after phase 3"):
        parse_phases(
            "## Phase 3: Later\nBody\n## Phase 2: Earlier\n",
            source="plan.md",
        )
