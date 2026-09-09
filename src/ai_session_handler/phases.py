"""Markdown plan phase parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import Final

PHASE_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(r"^#+ Phase ([0-9]+): (.+)$")
MARKDOWN_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(#+) (.+)$")
FENCE_OPEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


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


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    """One immutable read of an executable plan."""

    path: Path
    sha256: str
    preamble: str
    phases: tuple[Phase, ...]


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


@dataclass(frozen=True, slots=True)
class _Fence:
    marker: str
    length: int
    line: int


@dataclass(frozen=True, slots=True)
class _MarkdownLine:
    number: int
    text: str
    start_offset: int
    end_offset: int


def read_plan_snapshot(path: Path) -> PlanSnapshot:
    """Read, hash, and parse a plan from the same bytes."""
    canonical_path = path.resolve()
    plan_bytes = canonical_path.read_bytes()
    try:
        markdown = plan_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        line = plan_bytes[: error.start].count(b"\n") + 1
        raise PlanParseError(
            f"plan is not valid UTF-8 at byte {error.start + 1}",
            source=str(canonical_path),
            line=line,
        ) from error

    headings = _find_headings(markdown, source=str(canonical_path))
    phases = _parse_phases(markdown, headings=headings, source=str(canonical_path))
    preamble_end = headings[0].heading_start_offset
    return PlanSnapshot(
        path=canonical_path,
        sha256=sha256(plan_bytes).hexdigest(),
        preamble=markdown[:preamble_end],
        phases=tuple(phases),
    )


def parse_phase_file(path: Path) -> list[Phase]:
    """Read and parse phases from a markdown plan file."""
    return list(read_plan_snapshot(path).phases)


def parse_phases(markdown: str, *, source: str = "<string>") -> list[Phase]:
    """Parse explicitly marked markdown phases from plan text."""
    headings = _find_headings(markdown, source=source)
    return _parse_phases(markdown, headings=headings, source=source)


def _parse_phases(
    markdown: str,
    *,
    headings: list[_Heading],
    source: str,
) -> list[Phase]:
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
    lines = _unfenced_lines(body, source=source, line_offset=phase_heading.line)
    workspace_headings: list[tuple[int, int]] = []
    malformed_workspace_heading: int | None = None

    for index, line in enumerate(lines):
        match = MARKDOWN_HEADING_PATTERN.fullmatch(line.text)
        if match is None or not match.group(2).casefold().startswith("workspace"):
            continue
        if match.group(1) != "###" or match.group(2) != "Workspace":
            malformed_workspace_heading = line.number
            break
        workspace_headings.append((index, line.number))

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
        if MARKDOWN_HEADING_PATTERN.fullmatch(line.text) is not None:
            break
        if line.text.strip():
            content.append((line.text.strip(), line.number))

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


def _find_headings(markdown: str, *, source: str) -> list[_Heading]:
    headings: list[_Heading] = []
    for line in _unfenced_lines(markdown, source=source):
        match = PHASE_HEADING_PATTERN.fullmatch(line.text)
        if match is not None:
            number = int(match.group(1))
            headings.append(
                _Heading(
                    number=number,
                    title=match.group(2),
                    line=line.number,
                    body_start_offset=line.end_offset,
                    heading_start_offset=line.start_offset,
                )
            )
    return headings


def _unfenced_lines(
    markdown: str,
    *,
    source: str,
    line_offset: int = 0,
) -> list[_MarkdownLine]:
    lines: list[_MarkdownLine] = []
    fence: _Fence | None = None
    offset = 0
    for relative_number, raw_line in enumerate(markdown.splitlines(keepends=True), start=1):
        line_number = line_offset + relative_number
        text = raw_line.removesuffix("\n").removesuffix("\r")
        if fence is not None:
            if _is_closing_fence(text, fence):
                fence = None
        else:
            opening_fence = _opening_fence(text, line_number=line_number)
            if opening_fence is not None:
                fence = opening_fence
            else:
                lines.append(
                    _MarkdownLine(
                        number=line_number,
                        text=text,
                        start_offset=offset,
                        end_offset=offset + len(raw_line),
                    )
                )
        offset += len(raw_line)

    if fence is not None:
        marker_name = "backtick" if fence.marker == "`" else "tilde"
        raise PlanParseError(
            f"unclosed {marker_name} code fence",
            source=source,
            line=fence.line,
        )
    return lines


def _opening_fence(line: str, *, line_number: int) -> _Fence | None:
    match = FENCE_OPEN_PATTERN.fullmatch(line)
    if match is None:
        return None
    marker_text = match.group(1)
    if marker_text[0] == "`" and "`" in match.group(2):
        return None
    return _Fence(marker=marker_text[0], length=len(marker_text), line=line_number)


def _is_closing_fence(line: str, fence: _Fence) -> bool:
    return (
        re.fullmatch(rf" {{0,3}}{re.escape(fence.marker)}{{{fence.length},}}[ \t]*", line)
        is not None
    )


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
