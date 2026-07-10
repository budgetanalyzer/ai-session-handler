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


_MARKER_TAG_PATTERN: Final[str] = "|".join(re.escape(kind.value) for kind in MarkerKind)
_MARKER_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"<(?P<tag>{_MARKER_TAG_PATTERN})>(?P<text>.*?)</(?P=tag)>",
    re.DOTALL,
)
_MARKER_OPEN_PATTERN: Final[re.Pattern[str]] = re.compile(rf"<(?P<tag>{_MARKER_TAG_PATTERN})>")


@dataclass(slots=True)
class TerminalMarkerFilter:
    """Hide terminal marker blocks from incrementally streamed output."""

    open_tag: str | None = None

    def filter(self, text: str) -> str:
        visible_parts: list[str] = []
        remaining = text
        while remaining:
            if self.open_tag is not None:
                close_tag = f"</{self.open_tag}>"
                close_index = remaining.find(close_tag)
                if close_index == -1:
                    return "".join(visible_parts)
                remaining = remaining[close_index + len(close_tag) :]
                self.open_tag = None
                continue

            match = _MARKER_OPEN_PATTERN.search(remaining)
            if match is None:
                visible_parts.append(remaining)
                break

            visible_parts.append(remaining[: match.start()])
            self.open_tag = match.group("tag")
            remaining = remaining[match.end() :]

        return "".join(visible_parts)


def parse_terminal_marker(output: str) -> TerminalMarker:
    """Parse exactly one terminal marker from combined process output."""
    matches = list(_MARKER_PATTERN.finditer(output))
    if not matches:
        raise MissingMarkerError("missing terminal marker")
    if len(matches) > 1:
        raise MultipleMarkersError(f"expected one terminal marker, found {len(matches)}")

    match = matches[0]
    return TerminalMarker(kind=MarkerKind(match.group("tag")), text=match.group("text").strip())
