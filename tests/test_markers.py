"""Tests for terminal marker parsing."""

from __future__ import annotations

import pytest

from ai_session_handler.markers import (
    InvalidMarkerError,
    MarkerKind,
    MissingMarkerError,
    MultipleMarkersError,
    TerminalMarker,
    TerminalMarkerFilter,
    parse_terminal_marker,
)


def test_parse_complete_marker() -> None:
    assert parse_terminal_marker("done\n<phase-complete>Built it.</phase-complete>\n") == (
        TerminalMarker(kind=MarkerKind.COMPLETE, text="Built it.")
    )


def test_parse_blocked_marker() -> None:
    assert parse_terminal_marker("<phase-blocked>Need credentials.</phase-blocked>") == (
        TerminalMarker(kind=MarkerKind.BLOCKED, text="Need credentials.")
    )


def test_parse_needs_clarification_marker() -> None:
    assert parse_terminal_marker(
        "<phase-needs-clarification>Which API?</phase-needs-clarification>"
    ) == TerminalMarker(kind=MarkerKind.NEEDS_CLARIFICATION, text="Which API?")


def test_parse_multiline_marker() -> None:
    assert parse_terminal_marker(
        "progress\n<phase-complete>Implemented parser.\nAll tests pass.</phase-complete>\n"
    ) == TerminalMarker(
        kind=MarkerKind.COMPLETE,
        text="Implemented parser.\nAll tests pass.",
    )


def test_parse_marker_from_stderr() -> None:
    assert parse_terminal_marker(
        "ordinary stdout\n",
        "diagnostic\n<phase-blocked>Need credentials.</phase-blocked>\n",
    ) == TerminalMarker(kind=MarkerKind.BLOCKED, text="Need credentials.")


def test_missing_marker_is_rejected() -> None:
    with pytest.raises(MissingMarkerError):
        parse_terminal_marker("no terminal marker")


def test_multiple_markers_are_rejected() -> None:
    with pytest.raises(MultipleMarkersError):
        parse_terminal_marker(
            "<phase-complete>One</phase-complete><phase-blocked>Two</phase-blocked>"
        )


def test_markers_on_both_streams_are_rejected() -> None:
    with pytest.raises(MultipleMarkersError, match="both stdout and stderr"):
        parse_terminal_marker(
            "<phase-complete>Done.</phase-complete>\n",
            "<phase-blocked>Actually blocked.</phase-blocked>\n",
        )


@pytest.mark.parametrize(
    "output",
    [
        "quoted <phase-complete>fixture</phase-complete>\n",
        "<phase-complete>fixture</phase-complete>\nthen the operation failed\n",
        "<phase-complete>   </phase-complete>\n",
        "<phase-complete>unfinished\n",
        "</phase-complete>\n",
        "<phase-complete result\n",
        "```text\n<phase-complete>fixture</phase-complete>\n",
        "~~~\n<phase-blocked>fixture</phase-blocked>\n",
    ],
)
def test_invalid_marker_framing_is_rejected(output: str) -> None:
    with pytest.raises(InvalidMarkerError):
        parse_terminal_marker(output)


def test_nested_marker_is_rejected() -> None:
    with pytest.raises(MultipleMarkersError):
        parse_terminal_marker(
            "<phase-complete>outer\n<phase-blocked>inner</phase-blocked>\n</phase-complete>\n"
        )


def test_stream_filter_hides_marker_body_across_chunks() -> None:
    marker_filter = TerminalMarkerFilter()

    assert marker_filter.filter("before <phase-blocked>secret") == "before "
    assert marker_filter.filter(" detail") == ""
    assert marker_filter.filter("</phase-blocked> after\n") == " after\n"


def test_stream_filter_hides_split_tags_and_multiline_body() -> None:
    marker_filter = TerminalMarkerFilter()

    assert marker_filter.filter("before <phase-") == "before "
    assert marker_filter.filter("complete>secret\nsecond line") == ""
    assert marker_filter.filter("</phase-") == ""
    assert marker_filter.filter("complete> after") == " after"


def test_stream_filter_flushes_partial_nonmarker_at_eof() -> None:
    marker_filter = TerminalMarkerFilter()

    assert marker_filter.filter("ordinary <pha") == "ordinary "
    assert marker_filter.finish() == "<pha"
