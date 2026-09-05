"""Terminal marker parsing and live-output filtering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
    """Raised when process output does not contain one unambiguous final result."""


class MissingMarkerError(MarkerParseError):
    """Raised when neither output stream contains recognized marker text."""


class MultipleMarkersError(MarkerParseError):
    """Raised when output contains more than one recognized result block."""


class InvalidMarkerError(MarkerParseError):
    """Raised when recognized marker text does not follow the required framing."""


_MARKER_TAG_PATTERN: Final[str] = "|".join(re.escape(kind.value) for kind in MarkerKind)
_OPEN_TAGS: Final[tuple[str, ...]] = tuple(f"<{kind.value}>" for kind in MarkerKind)
_EXACT_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"<(?P<closing>/)?(?P<tag>{_MARKER_TAG_PATTERN})>"
)
_MARKER_LIKE_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"<\s*/?\s*(?:{_MARKER_TAG_PATTERN})(?=[\s>/]|$)[^\n>]*>?"
)
_BLOCK_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"<(?P<tag>{_MARKER_TAG_PATTERN})>(?P<text>.*?)</(?P=tag)>",
    re.DOTALL,
)
_FINAL_BLOCK_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^<(?P<tag>{_MARKER_TAG_PATTERN})>(?P<text>.*?)</(?P=tag)>\s*\Z",
    re.DOTALL | re.MULTILINE,
)
_FENCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})")


@dataclass(slots=True)
class TerminalMarkerFilter:
    """Hide terminal marker blocks from incrementally streamed output."""

    open_tag: str | None = None
    _pending: str = field(default="", repr=False)
    _hidden: str = field(default="", repr=False)

    def filter(self, text: str) -> str:
        """Return visible text while retaining partial tags for the next chunk."""
        self._pending += text
        visible_parts: list[str] = []

        while self._pending:
            if self.open_tag is not None:
                close_tag = f"</{self.open_tag}>"
                close_index = self._pending.find(close_tag)
                if close_index == -1:
                    retained = _possible_tag_prefix(self._pending, (close_tag,))
                    hidden_end = len(self._pending) - len(retained)
                    self._hidden += self._pending[:hidden_end]
                    self._pending = retained
                    break
                self._pending = self._pending[close_index + len(close_tag) :]
                self.open_tag = None
                self._hidden = ""
                continue

            match = _earliest_open_tag(self._pending)
            if match is None:
                retained = _possible_tag_prefix(self._pending, _OPEN_TAGS)
                visible_parts.append(self._pending[: len(self._pending) - len(retained)])
                self._pending = retained
                break

            start, tag = match
            visible_parts.append(self._pending[:start])
            self._pending = self._pending[start + len(tag) + 2 :]
            self.open_tag = tag
            self._hidden = f"<{tag}>"

        return "".join(visible_parts)

    def finish(self) -> str:
        """Flush trailing visible text when the emitting stream reaches EOF."""
        if self.open_tag is not None:
            pending = self._hidden + self._pending
            self._pending = ""
            self._hidden = ""
            self.open_tag = None
            return pending
        pending = self._pending
        self._pending = ""
        return pending


def parse_terminal_marker(stdout: str, stderr: str = "") -> TerminalMarker:
    """Parse one unambiguous final marker from either captured output stream."""
    markers = [
        marker
        for marker in (
            _parse_stream_marker(stdout, stream_name="stdout"),
            _parse_stream_marker(stderr, stream_name="stderr"),
        )
        if marker is not None
    ]
    if not markers:
        raise MissingMarkerError("missing terminal marker")
    if len(markers) > 1:
        raise MultipleMarkersError("terminal result was emitted on both stdout and stderr")
    return markers[0]


def _parse_stream_marker(output: str, *, stream_name: str) -> TerminalMarker | None:
    marker_like = list(_MARKER_LIKE_PATTERN.finditer(output))
    if not marker_like:
        return None

    exact_tags = list(_EXACT_TAG_PATTERN.finditer(output))
    complete_blocks = list(_BLOCK_PATTERN.finditer(output))
    if len(exact_tags) > 2 or len(complete_blocks) > 1:
        raise MultipleMarkersError(f"multiple terminal markers in {stream_name}")
    if len(exact_tags) != len(marker_like):
        raise InvalidMarkerError(f"malformed terminal marker in {stream_name}")
    if len(exact_tags) != 2:
        raise InvalidMarkerError(f"unclosed terminal marker in {stream_name}")

    match = _FINAL_BLOCK_PATTERN.search(output)
    if match is None:
        raise InvalidMarkerError(
            f"terminal marker in {stream_name} must begin at a line boundary "
            "and be its final content"
        )
    if exact_tags[0].start() != match.start() or exact_tags[1].end() != len(output.rstrip()):
        raise InvalidMarkerError(f"nested or mismatched terminal marker in {stream_name}")
    if _position_is_fenced(output, match.start()):
        raise InvalidMarkerError(f"terminal marker in {stream_name} is inside fenced text")

    text = match.group("text").strip()
    if not text:
        raise InvalidMarkerError(f"terminal marker in {stream_name} has an empty result")
    return TerminalMarker(kind=MarkerKind(match.group("tag")), text=text)


def _earliest_open_tag(text: str) -> tuple[int, str] | None:
    matches = (
        (index, kind.value) for kind in MarkerKind if (index := text.find(f"<{kind.value}>")) != -1
    )
    return min(matches, default=None)


def _possible_tag_prefix(text: str, tags: tuple[str, ...]) -> str:
    longest = min(len(text), max(len(tag) for tag in tags) - 1)
    for length in range(longest, 0, -1):
        suffix = text[-length:]
        if any(tag.startswith(suffix) for tag in tags):
            return suffix
    return ""


def _position_is_fenced(text: str, position: int) -> bool:
    fence_character: str | None = None
    fence_length = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        match = _FENCE_PATTERN.match(content)
        if match is not None:
            fence = match.group("fence")
            if fence_character is None:
                fence_character = fence[0]
                fence_length = len(fence)
            elif (
                fence[0] == fence_character
                and len(fence) >= fence_length
                and not content[match.end() :].strip()
            ):
                fence_character = None
                fence_length = 0
        if offset <= position < offset + len(line):
            return fence_character is not None
        offset += len(line)
    return False
