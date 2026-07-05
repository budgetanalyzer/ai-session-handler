"""Terminal marker parsing for worker process output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class MarkerKind(StrEnum):
    """Recognized terminal marker kinds emitted by workers."""

    COMPLETE = "phase-complete"
    BLOCKED = "phase-blocked"
    NEEDS_CLARIFICATION = "phase-needs-clarification"


@dataclass(frozen=True, slots=True)
class TerminalMarker:
    """One parsed terminal marker."""

    kind: MarkerKind
    text: str


class MarkerParseError(ValueError):
    """Raised when process output does not contain exactly one terminal marker."""


class MissingMarkerError(MarkerParseError):
    """Raised when no recognized terminal marker is present."""


class MultipleMarkersError(MarkerParseError):
    """Raised when more than one recognized terminal marker is present."""


_MARKER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"<(?P<tag>phase-complete|phase-blocked|phase-needs-clarification)>"
    r"(?P<text>.*?)"
    r"</(?P=tag)>",
    re.DOTALL,
)


def parse_terminal_marker(output: str) -> TerminalMarker:
    """Parse exactly one terminal marker from combined process output."""
    matches = list(_MARKER_PATTERN.finditer(output))
    if not matches:
        raise MissingMarkerError("missing terminal marker")
    if len(matches) > 1:
        raise MultipleMarkersError(f"expected one terminal marker, found {len(matches)}")

    match = matches[0]
    return TerminalMarker(kind=MarkerKind(match.group("tag")), text=match.group("text").strip())
