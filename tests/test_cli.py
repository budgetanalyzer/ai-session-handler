"""Smoke tests for the package entrypoints."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from pytest import CaptureFixture

from ai_session_handler import __version__
from ai_session_handler.cli import main
from ai_session_handler.runner import EXIT_INVALID


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


def test_init_creates_config_and_directories(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    exit_code = main(["init", "--workspace", str(tmp_path)])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "created" in captured.out
    assert (tmp_path / ".ai-session-handler" / "config.json").exists()
    assert (tmp_path / ".ai-session-handler" / "prompts").is_dir()
    assert (tmp_path / ".ai-session-handler" / "transcripts").is_dir()


def test_status_reports_next_phase(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("## Phase 1: One\nBody\n", encoding="utf-8")

    exit_code = main(["status", "--workspace", str(tmp_path), "--plan", "plan.md"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "next phase: phase-1 One" in captured.out
    assert "latest transcript: none" in captured.out


def test_status_reports_malformed_state_json(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("## Phase 1: One\nBody\n", encoding="utf-8")
    state_dir = tmp_path / ".ai-session-handler"
    state_dir.mkdir()
    (state_dir / "plan.json").write_text("{", encoding="utf-8")

    exit_code = main(["status", "--workspace", str(tmp_path), "--plan", "plan.md"])

    captured = capsys.readouterr()

    assert exit_code == EXIT_INVALID
    assert "invalid JSON" in captured.out
    assert captured.err == ""


def test_run_acceptance_with_fake_agent_subprocess(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("## Phase 1: One\nBody\n## Phase 2: Two\nBody\n", encoding="utf-8")
    agent_path = tmp_path / "agent.py"
    agent_path.write_text(
        "import sys\n"
        "prompt = sys.stdin.read()\n"
        "assert 'selected_phase_id: phase-1' in prompt\n"
        "print('<phase-complete>Subprocess complete.</phase-complete>')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_session_handler",
            "run",
            "--workspace",
            str(tmp_path),
            "--plan",
            "plan.md",
            "--agent-cmd",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(agent_path))}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "phase-complete: phase-1" in result.stdout
    assert (tmp_path / ".ai-session-handler" / "plan.json").exists()
