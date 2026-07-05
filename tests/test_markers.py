"""Tests for terminal marker parsing."""

from __future__ import annotations

import pytest

from ai_session_handler.markers import (
    MarkerKind,
    MissingMarkerError,
    MultipleMarkersError,
    TerminalMarker,
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


def test_missing_marker_is_rejected() -> None:
    with pytest.raises(MissingMarkerError):
        parse_terminal_marker("no terminal marker")


def test_multiple_markers_are_rejected() -> None:
    with pytest.raises(MultipleMarkersError):
        parse_terminal_marker(
            "<phase-complete>One</phase-complete><phase-blocked>Two</phase-blocked>"
        )
