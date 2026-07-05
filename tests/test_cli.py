"""Smoke tests for the package entrypoints."""

from __future__ import annotations

import subprocess
import sys

from pytest import CaptureFixture

from ai_session_handler import __version__
from ai_session_handler.cli import main


def test_main_prints_version(capsys: CaptureFixture[str]) -> None:
    exit_code = main(["--version"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == f"ai-session-handler {__version__}\n"
    assert captured.err == ""


def test_python_module_entrypoint_prints_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ai_session_handler", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Run one provider-agnostic AI agent plan phase and stop." in result.stdout
    assert result.stderr == ""
