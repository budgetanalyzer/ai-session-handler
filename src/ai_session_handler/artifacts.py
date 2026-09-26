"""Exclusive creation helpers for immutable attempt artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import TextIO


class ArtifactExistsError(FileExistsError):
    """Raised when an attempt would overwrite an existing artifact."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"{path}: attempt artifact already exists; refusing to overwrite it")


def open_text_exclusively(path: Path) -> TextIO:
    """Create and open a UTF-8 text artifact without replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return path.open("x", encoding="utf-8")
    except FileExistsError as error:
        raise ArtifactExistsError(path) from error


def write_text_exclusively(path: Path, text: str) -> None:
    """Create a UTF-8 text artifact and write its complete contents once."""
    with open_text_exclusively(path) as artifact:
        artifact.write(text)
