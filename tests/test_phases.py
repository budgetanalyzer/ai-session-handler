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
    with pytest.raises(PlanParseError, match=r"plan.md: expected at least one phase heading"):
        parse_phases("# Plan\n\nNo explicit phases.\n", source="plan.md")


@pytest.mark.parametrize(
    "heading",
    [
        "### Phase 1: Too Deep",
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
