"""Markdown plan phase parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PHASE_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(r"^#+ Phase ([0-9]+): (.+)$")


@dataclass(frozen=True, slots=True)
class Phase:
    """One explicitly marked phase from a markdown plan."""

    id: str
    number: int
    title: str
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
    return parse_phases(path.read_text(encoding="utf-8"), source=str(path))


def parse_phases(markdown: str, *, source: str = "<string>") -> list[Phase]:
    """Parse explicitly marked markdown phases from plan text."""
    headings = _find_headings(markdown)
    if not headings:
        raise PlanParseError("expected at least one phase heading", source=source)

    _validate_headings(headings, source=source)

    phases: list[Phase] = []
    for index, heading in enumerate(headings):
        next_heading = headings[index + 1] if index + 1 < len(headings) else None
        body_end_offset = (
            next_heading.heading_start_offset if next_heading is not None else len(markdown)
        )
        end_line = next_heading.line - 1 if next_heading is not None else _line_count(markdown)
        phases.append(
            Phase(
                id=f"phase-{heading.number}",
                number=heading.number,
                title=heading.title,
                body=markdown[heading.body_start_offset : body_end_offset],
                start_line=heading.line,
                end_line=end_line,
            )
        )

    return phases


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
    if markdown == "":
        return 1
    return len(markdown.splitlines())
