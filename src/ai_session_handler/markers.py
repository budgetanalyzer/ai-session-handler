"""Terminal marker parsing and live-output filtering."""

from __future__ import annotations

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


_OPEN_TAGS: Final[tuple[str, ...]] = tuple(f"<{kind.value}>" for kind in MarkerKind)


@dataclass(slots=True)
class _TagCandidate:
    line_boundary: bool
    fenced: bool
    stage: str = "after-open"
    closing: bool = False
    tag_prefix: str = ""
    exact: bool = True
    body_text: list[str] | None = None


@dataclass(slots=True)
class TerminalMarkerAccumulator:
    """Validate one output stream incrementally without retaining diagnostics."""

    _candidate: _TagCandidate | None = field(default=None, repr=False)
    _active_tag: MarkerKind | None = field(default=None, repr=False)
    _active_body: list[str] = field(default_factory=list, repr=False)
    _completed_marker: TerminalMarker | None = field(default=None, repr=False)
    _completed_framed: bool = field(default=False, repr=False)
    _exact_tag_count: int = field(default=0, repr=False)
    _complete_block_count: int = field(default=0, repr=False)
    _malformed: bool = field(default=False, repr=False)
    _non_whitespace_after_result: bool = field(default=False, repr=False)
    _at_line_boundary: bool = field(default=True, repr=False)
    _fence_character: str | None = field(default=None, repr=False)
    _fence_length: int = field(default=0, repr=False)
    _line_stage: str = field(default="leading", repr=False)
    _line_leading_spaces: int = field(default=0, repr=False)
    _line_fence_character: str | None = field(default=None, repr=False)
    _line_fence_length: int = field(default=0, repr=False)
    _line_fence_tail_non_whitespace: bool = field(default=False, repr=False)

    def feed(self, text: str) -> None:
        """Consume another decoded output chunk."""
        for character in text:
            line_boundary = self._at_line_boundary
            fenced = self._position_is_fenced()
            if (
                self._completed_marker is not None
                and self._active_tag is None
                and not character.isspace()
            ):
                self._non_whitespace_after_result = True
            self._consume_character(character, line_boundary=line_boundary, fenced=fenced)
            self._update_line_state(character)

    def finish(self, *, stream_name: str) -> TerminalMarker | None:
        """Finish the stream and return its marker or raise for invalid framing."""
        if self._candidate is not None:
            if self._candidate.stage == "after-tag":
                self._malformed = True
            self._append_candidate_to_body()
            self._candidate = None

        if self._exact_tag_count > 2 or self._complete_block_count > 1:
            raise MultipleMarkersError(f"multiple terminal markers in {stream_name}")
        if self._malformed:
            raise InvalidMarkerError(f"malformed terminal marker in {stream_name}")
        if self._exact_tag_count == 0:
            return None
        if self._exact_tag_count != 2:
            raise InvalidMarkerError(f"unclosed terminal marker in {stream_name}")
        if self._completed_marker is None or self._complete_block_count != 1:
            raise InvalidMarkerError(f"nested or mismatched terminal marker in {stream_name}")
        if not self._completed_framed or self._non_whitespace_after_result:
            raise InvalidMarkerError(
                f"terminal marker in {stream_name} must begin at a line boundary "
                "and be its final content"
            )
        if not self._completed_marker.text:
            raise InvalidMarkerError(f"terminal marker in {stream_name} has an empty result")
        return self._completed_marker

    def _consume_character(self, character: str, *, line_boundary: bool, fenced: bool) -> None:
        candidate = self._candidate
        if candidate is None:
            if character == "<":
                self._candidate = _TagCandidate(
                    line_boundary=line_boundary,
                    fenced=fenced,
                    body_text=[] if self._active_tag is not None else None,
                )
            elif self._active_tag is not None:
                self._active_body.append(character)
            return

        if candidate.body_text is not None:
            candidate.body_text.append(character)

        if candidate.stage == "after-open":
            if character.isspace():
                candidate.exact = False
                return
            if character == "/":
                candidate.closing = True
                candidate.stage = "after-slash"
                return
            if character == "p":
                candidate.tag_prefix = character
                candidate.stage = "tag"
                return
            self._fail_candidate(character, line_boundary=line_boundary, fenced=fenced)
            return

        if candidate.stage == "after-slash":
            if character.isspace():
                candidate.exact = False
                return
            if character == "p":
                candidate.tag_prefix = character
                candidate.stage = "tag"
                return
            self._fail_candidate(character, line_boundary=line_boundary, fenced=fenced)
            return

        if candidate.stage == "tag":
            prefix = candidate.tag_prefix + character
            matching_tags = [kind for kind in MarkerKind if kind.value.startswith(prefix)]
            if not matching_tags:
                self._fail_candidate(character, line_boundary=line_boundary, fenced=fenced)
                return
            candidate.tag_prefix = prefix
            if any(kind.value == prefix for kind in matching_tags):
                candidate.stage = "after-tag"
            return

        if character.isspace() or character in {"/", ">"}:
            is_exact = candidate.exact and character == ">"
            if is_exact:
                self._accept_exact_tag(candidate)
            else:
                self._malformed = True
                self._append_candidate_to_body()
            self._candidate = None
            return
        self._fail_candidate(character, line_boundary=line_boundary, fenced=fenced)

    def _fail_candidate(self, character: str, *, line_boundary: bool, fenced: bool) -> None:
        candidate = self._candidate
        assert candidate is not None
        if candidate.body_text is not None:
            self._active_body.append("<")
            self._active_body.extend(candidate.body_text)
        self._candidate = None
        if character == "<":
            if self._active_tag is not None:
                self._active_body.pop()
            self._candidate = _TagCandidate(
                line_boundary=line_boundary,
                fenced=fenced,
                body_text=[] if self._active_tag is not None else None,
            )

    def _append_candidate_to_body(self) -> None:
        candidate = self._candidate
        if candidate is not None and candidate.body_text is not None:
            self._active_body.append("<")
            self._active_body.extend(candidate.body_text)

    def _accept_exact_tag(self, candidate: _TagCandidate) -> None:
        kind = MarkerKind(candidate.tag_prefix)
        self._exact_tag_count += 1
        if candidate.closing:
            if self._active_tag is kind:
                text = "".join(self._active_body).strip()
                self._completed_marker = TerminalMarker(kind=kind, text=text)
                self._complete_block_count += 1
                self._active_tag = None
                self._active_body = []
            else:
                self._append_candidate_to_body()
            return

        if self._active_tag is None:
            self._active_tag = kind
            self._active_body = []
            self._completed_framed = candidate.line_boundary and not candidate.fenced
        else:
            self._append_candidate_to_body()

    def _position_is_fenced(self) -> bool:
        if self._fence_character is not None:
            return True
        return self._line_fence_length >= 3

    def _update_line_state(self, character: str) -> None:
        if character == "\n":
            if self._line_fence_length >= 3:
                fence_character = self._line_fence_character
                assert fence_character is not None
                if self._fence_character is None:
                    self._fence_character = fence_character
                    self._fence_length = self._line_fence_length
                elif (
                    fence_character == self._fence_character
                    and self._line_fence_length >= self._fence_length
                    and not self._line_fence_tail_non_whitespace
                ):
                    self._fence_character = None
                    self._fence_length = 0
            self._line_stage = "leading"
            self._line_leading_spaces = 0
            self._line_fence_character = None
            self._line_fence_length = 0
            self._line_fence_tail_non_whitespace = False
            self._at_line_boundary = True
            return

        if self._line_stage == "leading":
            if character in {" ", "\t"} and self._line_leading_spaces < 3:
                self._line_leading_spaces += 1
            elif character in {"`", "~"}:
                self._line_stage = "fence"
                self._line_fence_character = character
                self._line_fence_length = 1
            else:
                self._line_stage = "tail"
        elif self._line_stage == "fence":
            if character == self._line_fence_character:
                self._line_fence_length += 1
            else:
                self._line_stage = "tail"
                if not character.isspace():
                    self._line_fence_tail_non_whitespace = True
        elif not character.isspace():
            self._line_fence_tail_non_whitespace = True
        self._at_line_boundary = False


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
    stdout_accumulator = TerminalMarkerAccumulator()
    stderr_accumulator = TerminalMarkerAccumulator()
    stdout_accumulator.feed(stdout)
    stderr_accumulator.feed(stderr)
    return parse_accumulated_terminal_marker(stdout_accumulator, stderr_accumulator)


def parse_accumulated_terminal_marker(
    stdout: TerminalMarkerAccumulator,
    stderr: TerminalMarkerAccumulator,
) -> TerminalMarker:
    """Parse one marker from two completed incremental stream accumulators."""
    markers = [
        marker
        for marker in (
            stdout.finish(stream_name="stdout"),
            stderr.finish(stream_name="stderr"),
        )
        if marker is not None
    ]
    if not markers:
        raise MissingMarkerError("missing terminal marker")
    if len(markers) > 1:
        raise MultipleMarkersError("terminal result was emitted on both stdout and stderr")
    return markers[0]


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
