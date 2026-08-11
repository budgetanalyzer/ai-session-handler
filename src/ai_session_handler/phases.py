"""Markdown plan phase parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Final

PHASE_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(r"^#+ Phase ([0-9]+): (.+)$")
MARKDOWN_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(#+) (.+)$")


@dataclass(frozen=True, slots=True)
class Phase:
    """One explicitly marked phase from a markdown plan."""

    id: str
    number: int
    title: str
    workspace: str
    workspace_line: int
    body: str
    start_line: int
    end_line: int


class PlanParseError(ValueError):
    """Raised when a markdown plan cannot be parsed into valid phases."""

    def __init__(self, message: str, *, source: str, line: int | None = None) -> None:
        self.source = source
        self.line = line
        location = source if line is None else f"{source}:{line}"
        super().__init__(f"{location}: {message}")


@dataclass(frozen=True, slots=True)
class _Heading:
    number: int
    title: str
    line: int
    body_start_offset: int
    heading_start_offset: int


def parse_phase_file(path: Path) -> list[Phase]:
    """Read and parse phases from a markdown plan file."""
    return parse_phases(path.read_bytes().decode("utf-8"), source=str(path))


def parse_phases(markdown: str, *, source: str = "<string>") -> list[Phase]:
    """Parse explicitly marked markdown phases from plan text."""
    headings = _find_headings(markdown)
    if not headings:
        raise PlanParseError(
            "expected at least one executable phase heading like '## Phase 1: Title'",
            source=source,
        )

    _validate_headings(headings, source=source)

    phases: list[Phase] = []
    for index, heading in enumerate(headings):
        next_heading = headings[index + 1] if index + 1 < len(headings) else None
        body_end_offset = (
            next_heading.heading_start_offset if next_heading is not None else len(markdown)
        )
        end_line = next_heading.line - 1 if next_heading is not None else _line_count(markdown)
        body = markdown[heading.body_start_offset : body_end_offset]
        workspace, workspace_line = _parse_workspace(
            body,
            source=source,
            phase_heading=heading,
        )
        phases.append(
            Phase(
                id=f"phase-{heading.number}",
                number=heading.number,
                title=heading.title,
                workspace=workspace,
                workspace_line=workspace_line,
                body=body,
                start_line=heading.line,
                end_line=end_line,
            )
        )

    return phases


def resolve_phase_workspace(
    phase: Phase,
    *,
    plan_workspace_path: Path,
    source: str,
) -> Path:
    """Resolve and validate one phase's execution workspace."""
    execution_workspace_path = (plan_workspace_path / phase.workspace).resolve()
    phase_label = f"phase {phase.id} workspace {phase.workspace!r}"

    if not execution_workspace_path.is_dir():
        raise PlanParseError(
            f"{phase_label} resolves to {execution_workspace_path}, which is not an existing "
            "directory",
            source=source,
            line=phase.workspace_line,
        )
    instructions_path = execution_workspace_path / "AGENTS.md"
    if not instructions_path.is_file():
        raise PlanParseError(
            f"{phase_label} resolves to {execution_workspace_path}, which does not contain "
            "AGENTS.md at its root",
            source=source,
            line=phase.workspace_line,
        )
    return execution_workspace_path


def _parse_workspace(
    body: str,
    *,
    source: str,
    phase_heading: _Heading,
) -> tuple[str, int]:
    lines = body.splitlines()
    workspace_headings: list[tuple[int, int]] = []
    malformed_workspace_heading: int | None = None

    for index, line in enumerate(lines):
        match = MARKDOWN_HEADING_PATTERN.fullmatch(line)
        if match is None or not match.group(2).casefold().startswith("workspace"):
            continue
        line_number = phase_heading.line + index + 1
        if match.group(1) != "###" or match.group(2) != "Workspace":
            malformed_workspace_heading = line_number
            break
        workspace_headings.append((index, line_number))

    if malformed_workspace_heading is not None:
        raise PlanParseError(
            "workspace section must use the exact heading '### Workspace'",
            source=source,
            line=malformed_workspace_heading,
        )
    if not workspace_headings:
        raise PlanParseError(
            f"phase {phase_heading.number} is missing required '### Workspace' section",
            source=source,
            line=phase_heading.line,
        )
    if len(workspace_headings) > 1:
        raise PlanParseError(
            f"phase {phase_heading.number} has duplicate '### Workspace' sections",
            source=source,
            line=workspace_headings[1][1],
        )

    heading_index, heading_line = workspace_headings[0]
    content: list[tuple[str, int]] = []
    for index in range(heading_index + 1, len(lines)):
        line = lines[index]
        if MARKDOWN_HEADING_PATTERN.fullmatch(line) is not None:
            break
        if line.strip():
            content.append((line.strip(), phase_heading.line + index + 1))

    if not content:
        raise PlanParseError(
            f"phase {phase_heading.number} has an empty workspace declaration",
            source=source,
            line=heading_line,
        )
    if len(content) > 1:
        raise PlanParseError(
            "workspace declaration must contain exactly one relative path line",
            source=source,
            line=content[1][1],
        )

    workspace, workspace_line = content[0]
    if Path(workspace).is_absolute() or PureWindowsPath(workspace).is_absolute():
        raise PlanParseError(
            f"workspace path must be relative, got {workspace!r}",
            source=source,
            line=workspace_line,
        )
    return workspace, workspace_line


def _find_headings(markdown: str) -> list[_Heading]:
    headings: list[_Heading] = []
    offset = 0
    for line_number, line in enumerate(markdown.splitlines(keepends=True), start=1):
        line_without_ending = line.removesuffix("\n").removesuffix("\r")
        match = PHASE_HEADING_PATTERN.fullmatch(line_without_ending)
        if match is not None:
            number = int(match.group(1))
            headings.append(
                _Heading(
                    number=number,
                    title=match.group(2),
                    line=line_number,
                    body_start_offset=offset + len(line),
                    heading_start_offset=offset,
                )
            )
        offset += len(line)
    return headings


def _validate_headings(headings: list[_Heading], *, source: str) -> None:
    seen_numbers: set[int] = set()
    previous_number: int | None = None

    for heading in headings:
        if heading.number < 1:
            raise PlanParseError(
                "phase number must be positive",
                source=source,
                line=heading.line,
            )
        if heading.number in seen_numbers:
            raise PlanParseError(
                f"duplicate phase number {heading.number}",
                source=source,
                line=heading.line,
            )
        if previous_number is not None and heading.number <= previous_number:
            raise PlanParseError(
                f"phase number {heading.number} appears after phase {previous_number}",
                source=source,
                line=heading.line,
            )

        seen_numbers.add(heading.number)
        previous_number = heading.number


def _line_count(markdown: str) -> int:
    return len(markdown.splitlines())
