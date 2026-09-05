"""Smoke tests for the package entrypoints."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest
from pytest import CaptureFixture, MonkeyPatch

from ai_session_handler import __version__
from ai_session_handler.cli import main
from ai_session_handler.config import default_state_path, plan_generated_path
from ai_session_handler.runner import EXIT_AGENT_FAILED, EXIT_BLOCKED, EXIT_INVALID
from ai_session_handler.state import ActiveAttemptStatus, RunnerState, read_state, write_state


@pytest.fixture(autouse=True)
def _plan_repository_instructions(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# Test repository\n", encoding="utf-8")


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
    assert "Run a provider-agnostic AI agent plan to completion." in result.stdout
    assert result.stderr == ""


def test_init_creates_config_and_directories(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = main(["init"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "created" in captured.out
    assert (tmp_path / ".ai-session-handler" / "config.json").exists()
    assert (tmp_path / ".ai-session-handler" / "plans").is_dir()
    assert not (tmp_path / ".ai-session-handler" / "prompts").exists()
    assert not (tmp_path / ".ai-session-handler" / "transcripts").exists()
    config = json.loads(
        (tmp_path / ".ai-session-handler" / "config.json").read_text(encoding="utf-8")
    )
    assert config["max_phases"] is None


@pytest.mark.parametrize(
    ("argv", "option"),
    [
        (["init", "--config", "custom.json"], "--config"),
        (["status", "--plan", "plan.md", "--state", "custom.json"], "--state"),
        (["run", "--plan", "plan.md", "--config", "custom.json"], "--config"),
    ],
)
def test_removed_path_options_are_rejected(
    argv: list[str],
    option: str,
    capsys: CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(argv)

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert f"unrecognized arguments: {option}" in captured.err


def test_status_reports_next_phase(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["status", "--plan", "plan.md"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "next phase: phase-1 One" in captured.out
    assert f"plan workspace path: {tmp_path}" in captured.out
    assert f"state path: {default_state_path(tmp_path, plan_path)}" in captured.out
    assert f"execution workspace path: {tmp_path}" in captured.out
    assert "latest transcript: none" in captured.out


def test_run_and_status_resolve_plan_relative_to_nested_caller_directory(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    workspace = tmp_path / "repository"
    caller = workspace / "tools"
    plan_path = workspace / "docs" / "plans" / "plan.md"
    agent_path = workspace / "agent.py"
    caller.mkdir(parents=True)
    plan_path.parent.mkdir(parents=True)
    (workspace / "AGENTS.md").write_text("# Test repository\n", encoding="utf-8")
    plan_path.write_text(_phase(), encoding="utf-8")
    agent_path.write_text(
        "print('<phase-complete>Subprocess complete.</phase-complete>')\n",
        encoding="utf-8",
    )
    relative_plan = Path("../docs/plans/plan.md")
    monkeypatch.chdir(caller)

    run_exit_code = main(
        [
            "run",
            "--plan",
            str(relative_plan),
            "--agent-cmd",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(agent_path))}",
        ]
    )
    capsys.readouterr()
    status_exit_code = main(["status", "--plan", str(relative_plan)])

    captured = capsys.readouterr()
    state_path = default_state_path(workspace, plan_path)
    assert run_exit_code == 0
    assert status_exit_code == 0
    assert plan_path.resolve() == plan_path
    assert read_state(state_path).completed_phase_ids == ("phase-1",)
    assert "all complete" in captured.out
    assert f"plan workspace path: {workspace}" in captured.out


def test_status_resolves_relative_plan_in_sibling_repository_with_spaces(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    caller = tmp_path / "caller"
    workspace = tmp_path / "target repository"
    plan_path = workspace / "docs" / "plans" / "plan with spaces.md"
    caller.mkdir()
    plan_path.parent.mkdir(parents=True)
    (workspace / "AGENTS.md").write_text("# Target repository\n", encoding="utf-8")
    plan_path.write_text(_phase(), encoding="utf-8")
    monkeypatch.chdir(caller)

    exit_code = main(["status", "--plan", "../target repository/docs/plans/plan with spaces.md"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"plan workspace path: {workspace}" in captured.out
    assert f"execution workspace path: {workspace}" in captured.out


def test_status_reports_malformed_state_json(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(), encoding="utf-8")
    state_path = default_state_path(tmp_path, plan_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["status", "--plan", "plan.md"])

    captured = capsys.readouterr()

    assert exit_code == EXIT_INVALID
    assert captured.out == ""
    assert "invalid JSON" in captured.err


def test_run_reports_malformed_config_json_with_path(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(), encoding="utf-8")
    config_path = tmp_path / ".ai-session-handler" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text("{", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["run", "--plan", "plan.md"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_INVALID
    assert captured.out == ""
    assert str(config_path) in captured.err
    assert "invalid JSON at line 1, column 2" in captured.err


def test_invalid_command_template_returns_input_error_without_launching_worker(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(), encoding="utf-8")
    sentinel_path = tmp_path / "launched"
    agent_path = tmp_path / "agent.py"
    agent_path.write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel_path)!r}).touch()\n"
        "print('<phase-complete>Unexpected.</phase-complete>')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "run",
            "--plan",
            "plan.md",
            "--agent-cmd",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(agent_path))} {{}}",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == EXIT_INVALID
    assert captured.out == ""
    assert "positional command placeholder is not allowed: {}" in captured.err
    assert "Traceback" not in captured.err
    assert not sentinel_path.exists()
    assert not default_state_path(tmp_path, plan_path).exists()
    assert not (tmp_path / ".ai-session-handler" / "prompts").exists()
    assert not (tmp_path / ".ai-session-handler" / "transcripts").exists()


def test_plan_edit_during_run_returns_hash_mismatch_after_recording_outcome(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(), encoding="utf-8")
    agent_path = tmp_path / "agent.py"
    agent_path.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "sys.stdin.read()\n"
        f"plan_path = Path({str(plan_path)!r})\n"
        "plan_path.write_text(plan_path.read_text(encoding='utf-8') + '\\nEdited.\\n', "
        "encoding='utf-8')\n"
        "print('<phase-complete>Completed accepted snapshot.</phase-complete>')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "run",
            "--plan",
            "plan.md",
            "--agent-cmd",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(agent_path))}",
        ]
    )

    captured = capsys.readouterr()
    state = read_state(default_state_path(tmp_path, plan_path))
    assert exit_code == EXIT_INVALID
    assert captured.out.strip() == ""
    assert f"plan hash mismatch: {plan_path}" in captured.err
    assert "expected:" in captured.err
    assert "actual:" in captured.err
    assert state.completed_phase_ids == ("phase-1",)
    assert state.last_run is not None
    assert state.last_run.summary == "Completed accepted snapshot."


def test_run_agent_failure_reports_error_details_to_stderr(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(), encoding="utf-8")
    agent_path = tmp_path / "agent.py"
    agent_path.write_text("import sys\nsys.exit(7)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "run",
            "--plan",
            "plan.md",
            "--agent-cmd",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(agent_path))}",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == EXIT_AGENT_FAILED
    assert captured.out == ""
    assert "agent-failed: agent command exited with code 7" in captured.err
    assert "transcript:" in captured.err
    assert f"plan_workspace_path: {tmp_path}" in captured.err
    assert f"execution_workspace_path: {tmp_path}" in captured.err
    assert f"argv: {shlex.quote(sys.executable)} {shlex.quote(str(agent_path))}" in captured.err
    assert "[runner] process exited with code 7 without stdout/stderr output" in captured.err


def test_run_streams_progress_and_prints_terminal_summary_once(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(), encoding="utf-8")
    agent_path = tmp_path / "agent.py"
    agent_path.write_text(
        "print('working...')\n"
        "print('<phase-blocked>Need Auth0 env.\\nSet AUTH0_MGMT_DOMAIN.</phase-blocked>')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "run",
            "--plan",
            "plan.md",
            "--agent-cmd",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(agent_path))}",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == EXIT_BLOCKED
    assert "working..." in captured.out
    assert "<phase-blocked>" not in captured.out
    assert "</phase-blocked>" not in captured.out
    assert captured.out.count("Need Auth0 env.") == 1
    assert captured.out.count("Set AUTH0_MGMT_DOMAIN.") == 1


def test_run_quiet_suppresses_progress_but_preserves_transcript_and_summary(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(), encoding="utf-8")
    agent_path = tmp_path / "agent.py"
    agent_path.write_text(
        "import sys\n"
        "print('working quietly')\n"
        "print('diagnostic detail', file=sys.stderr)\n"
        "print('<phase-complete>Implemented quietly.</phase-complete>')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "run",
            "--plan",
            "plan.md",
            "--agent-cmd",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(agent_path))}",
            "--quiet",
        ]
    )

    captured = capsys.readouterr()
    state = read_state(default_state_path(tmp_path, plan_path))
    assert state.last_run is not None
    transcript = Path(state.last_run.transcript_path).read_text(encoding="utf-8")

    assert exit_code == 0
    assert captured.out == "runner-complete: all phases complete\n"
    assert captured.err == ""
    assert "working quietly" in transcript
    assert "diagnostic detail" in transcript
    assert "<phase-complete>Implemented quietly.</phase-complete>" in transcript


def test_run_stopped_agent_failure_reports_transcript_tail(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(), encoding="utf-8")
    agent_path = tmp_path / "agent.py"
    agent_path.write_text(
        "import sys\n"
        "print('Traceback (most recent call last):', file=sys.stderr)\n"
        "print('RuntimeError: boom', file=sys.stderr)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(agent_path))}"
    monkeypatch.chdir(tmp_path)

    first_exit_code = main(
        [
            "run",
            "--plan",
            "plan.md",
            "--agent-cmd",
            command,
        ]
    )
    capsys.readouterr()

    second_exit_code = main(
        [
            "run",
            "--plan",
            "plan.md",
            "--agent-cmd",
            command,
        ]
    )

    captured = capsys.readouterr()

    assert first_exit_code == EXIT_AGENT_FAILED
    assert second_exit_code == EXIT_INVALID
    assert captured.out == ""
    assert "error: phase phase-1 is stopped: agent-failed" in captured.err
    assert "message: agent command exited with code 1" in captured.err
    assert f"plan workspace path: {tmp_path}" in captured.err
    assert f"execution workspace path: {tmp_path}" in captured.err
    assert "transcript:" in captured.err
    assert "transcript tail" in captured.err
    assert "Traceback (most recent call last):" in captured.err
    assert "RuntimeError: boom" in captured.err


def test_run_acceptance_with_fake_agent_subprocess(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase() + _phase(title="Two", number=2), encoding="utf-8")
    agent_path = tmp_path / "agent.py"
    agent_path.write_text(
        "import sys\n"
        "prompt = sys.stdin.read()\n"
        "assert 'selected_phase_id: phase-' in prompt\n"
        "print('<phase-complete>Subprocess complete.</phase-complete>')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_session_handler",
            "run",
            "--plan",
            "plan.md",
            "--agent-cmd",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(agent_path))}",
        ],
        check=False,
        capture_output=True,
        cwd=tmp_path,
        text=True,
    )

    assert result.returncode == 0
    assert "runner-complete: all phases complete" in result.stdout
    state = read_state(default_state_path(tmp_path, plan_path))
    assert state.completed_phase_ids == ("phase-1", "phase-2")


def test_run_max_phases_one_stops_after_one_phase(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase() + _phase(title="Two", number=2), encoding="utf-8")
    agent_path = tmp_path / "agent.py"
    agent_path.write_text(
        "print('<phase-complete>Subprocess complete.</phase-complete>')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_session_handler",
            "run",
            "--plan",
            "plan.md",
            "--agent-cmd",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(agent_path))}",
            "--max-phases",
            "1",
        ],
        check=False,
        capture_output=True,
        cwd=tmp_path,
        text=True,
    )

    assert result.returncode == 0
    assert "phase-complete: phase-1" in result.stdout
    state = read_state(default_state_path(tmp_path, plan_path))
    assert state.completed_phase_ids == ("phase-1",)


def test_run_infers_workspace_from_absolute_plan_path(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = tmp_path / "target-repo"
    other_cwd = tmp_path / "handler-repo"
    plan_path = workspace / "docs" / "plans" / "plan.md"
    state_path = default_state_path(workspace, plan_path)
    agent_path = workspace / "agent.py"
    config_path = workspace / ".ai-session-handler" / "config.json"

    plan_path.parent.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    other_cwd.mkdir()
    (workspace / "AGENTS.md").write_text("# Target repository\n", encoding="utf-8")
    plan_path.write_text(_phase() + _phase(title="Two", number=2), encoding="utf-8")
    agent_path.write_text(
        "import sys\n"
        "prompt = sys.stdin.read()\n"
        f"assert 'plan_workspace_path: {workspace}' in prompt\n"
        f"assert 'execution_workspace_path: {workspace}' in prompt\n"
        "print('<phase-complete>Subprocess complete.</phase-complete>')\n",
        encoding="utf-8",
    )
    config_path.write_text(
        "{\n"
        f'  "agent_cmd": "{shlex.quote(sys.executable)} {shlex.quote(str(agent_path))}",\n'
        '  "max_phases": 2,\n'
        '  "timeout_seconds": 3600,\n'
        '  "stop_on_regex": []\n'
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(other_cwd)

    exit_code = main(["run", "--plan", str(plan_path)])

    assert exit_code == 0
    assert state_path.exists()
    assert read_state(state_path).completed_phase_ids == ("phase-1", "phase-2")


def test_same_stem_same_content_plans_keep_separate_history(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    first_plan = tmp_path / "docs" / "first" / "plan.md"
    second_plan = tmp_path / "docs" / "second" / "plan.md"
    first_plan.parent.mkdir(parents=True)
    second_plan.parent.mkdir(parents=True)
    first_plan.write_text(_phase(), encoding="utf-8")
    second_plan.write_text(_phase(), encoding="utf-8")
    agent_path = tmp_path / "agent.py"
    agent_path.write_text(
        "print('<phase-complete>Distinct history.</phase-complete>')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    agent_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(agent_path))}"

    first_exit = main(["run", "--plan", str(first_plan), "--agent-cmd", agent_cmd])
    capsys.readouterr()
    second_exit = main(["run", "--plan", str(second_plan), "--agent-cmd", agent_cmd])
    capsys.readouterr()

    first_state_path = default_state_path(tmp_path, first_plan)
    second_state_path = default_state_path(tmp_path, second_plan)
    assert first_exit == 0
    assert second_exit == 0
    assert first_state_path != second_state_path
    first_state = read_state(first_state_path)
    second_state = read_state(second_state_path)
    assert first_state.plan is not None
    assert first_state.plan.path == str(first_plan)
    assert second_state.plan is not None
    assert second_state.plan.path == str(second_plan)
    assert len(list((plan_generated_path(tmp_path, first_plan) / "prompts").glob("*.txt"))) == 1
    assert len(list((plan_generated_path(tmp_path, second_plan) / "prompts").glob("*.txt"))) == 1


def test_status_rejects_legacy_state_without_modifying_it(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(), encoding="utf-8")
    legacy_path = tmp_path / ".ai-session-handler" / "plan.json"
    write_state(legacy_path, RunnerState())
    original = legacy_path.read_bytes()
    monkeypatch.chdir(tmp_path)

    exit_code = main(["status", "--plan", "plan.md"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_INVALID
    assert "legacy plan state detected" in captured.err
    assert "docs/state-and-recovery.md" in captured.err
    assert legacy_path.read_bytes() == original
    assert not default_state_path(tmp_path, plan_path).exists()


def test_config_named_plan_detects_legacy_state_config_collision(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    plan_path = tmp_path / "config.md"
    plan_path.write_text(_phase(), encoding="utf-8")
    collision_path = tmp_path / ".ai-session-handler" / "config.json"
    write_state(collision_path, RunnerState())
    original = collision_path.read_bytes()
    monkeypatch.chdir(tmp_path)

    exit_code = main(["status", "--plan", "config.md"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_INVALID
    assert "legacy plan state detected" in captured.err
    assert str(collision_path) in captured.err
    assert collision_path.read_bytes() == original
    assert not default_state_path(tmp_path, plan_path).exists()


def test_config_named_plan_allows_normal_shared_config(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    plan_path = tmp_path / "config.md"
    plan_path.write_text(_phase(), encoding="utf-8")
    config_path = tmp_path / ".ai-session-handler" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text('{"agent_cmd": null}\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["status", "--plan", "config.md"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "next phase: phase-1 One" in captured.out
    assert captured.err == ""


def test_competing_invocation_cannot_launch_same_phase_or_change_state(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(), encoding="utf-8")
    ready_path = tmp_path / "holder-ready"
    holder = _write_blocking_agent(tmp_path, ready_path)
    sentinel_path = tmp_path / "contender-launched"
    contender = tmp_path / "contender.py"
    contender.write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel_path)!r}).touch()\n"
        "print('<phase-complete>Contender ran.</phase-complete>')\n",
        encoding="utf-8",
    )
    holder_process = _start_handler(tmp_path, plan_path, holder)

    try:
        _wait_for_readiness(holder_process, ready_path)
        state_path = default_state_path(tmp_path, plan_path)
        state_before_contention = state_path.read_bytes()

        contender_result = _run_handler(tmp_path, plan_path, contender)

        assert contender_result.returncode == EXIT_INVALID
        assert "plan workspace" in contender_result.stderr
        assert "already owned by another ai-session-handler invocation" in contender_result.stderr
        assert not sentinel_path.exists()
        assert state_path.read_bytes() == state_before_contention
    finally:
        _terminate_handler(holder_process)

    released_result = _run_handler(
        tmp_path,
        plan_path,
        contender,
        extra_cli_args=("--retry-stopped",),
    )
    assert released_result.returncode == 0
    assert sentinel_path.exists()


def test_separate_plans_in_one_plan_workspace_serialize(tmp_path: Path) -> None:
    first_plan = tmp_path / "first.md"
    second_plan = tmp_path / "second.md"
    first_plan.write_text(_phase(), encoding="utf-8")
    second_plan.write_text(_phase(), encoding="utf-8")
    ready_path = tmp_path / "holder-ready"
    holder = _write_blocking_agent(tmp_path, ready_path)
    sentinel_path = tmp_path / "second-plan-launched"
    contender = tmp_path / "contender.py"
    contender.write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel_path)!r}).touch()\n"
        "print('<phase-complete>Unexpected.</phase-complete>')\n",
        encoding="utf-8",
    )
    holder_process = _start_handler(tmp_path, first_plan, holder)

    try:
        _wait_for_readiness(holder_process, ready_path)

        contender_result = _run_handler(tmp_path, second_plan, contender)

        assert contender_result.returncode == EXIT_INVALID
        assert f"plan workspace {tmp_path}" in contender_result.stderr
        assert not sentinel_path.exists()
        assert not default_state_path(tmp_path, second_plan).exists()
    finally:
        _terminate_handler(holder_process)


def test_plans_from_different_roots_serialize_on_shared_execution_workspace(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first-root"
    second_root = tmp_path / "second-root"
    execution_workspace = tmp_path / "shared-service"
    for workspace in (first_root, second_root, execution_workspace):
        workspace.mkdir()
        (workspace / "AGENTS.md").write_text("# Test repository\n", encoding="utf-8")
    first_plan = first_root / "plan.md"
    second_plan = second_root / "plan.md"
    execution_alias = tmp_path / "shared-service-alias"
    execution_alias.symlink_to(execution_workspace, target_is_directory=True)
    first_plan.write_text(_phase(workspace="../shared-service"), encoding="utf-8")
    second_plan.write_text(_phase(workspace="../shared-service-alias"), encoding="utf-8")
    ready_path = tmp_path / "holder-ready"
    holder = _write_blocking_agent(tmp_path, ready_path)
    sentinel_path = tmp_path / "second-root-launched"
    contender = tmp_path / "contender.py"
    contender.write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel_path)!r}).touch()\n"
        "print('<phase-complete>Unexpected.</phase-complete>')\n",
        encoding="utf-8",
    )
    holder_process = _start_handler(first_root, first_plan, holder)

    try:
        _wait_for_readiness(holder_process, ready_path)

        contender_result = _run_handler(second_root, second_plan, contender)

        assert contender_result.returncode == EXIT_INVALID
        assert f"execution workspace {execution_workspace}" in contender_result.stderr
        assert not sentinel_path.exists()
        assert not default_state_path(second_root, second_plan).exists()
    finally:
        _terminate_handler(holder_process)


def test_execution_contention_does_not_clear_stopped_retry_state(tmp_path: Path) -> None:
    retry_root = tmp_path / "retry-root"
    holder_root = tmp_path / "holder-root"
    execution_workspace = tmp_path / "shared-service"
    for workspace in (retry_root, holder_root, execution_workspace):
        workspace.mkdir()
        (workspace / "AGENTS.md").write_text("# Test repository\n", encoding="utf-8")
    retry_plan = retry_root / "plan.md"
    holder_plan = holder_root / "plan.md"
    retry_plan.write_text(_phase(workspace="../shared-service"), encoding="utf-8")
    holder_plan.write_text(_phase(workspace="../shared-service"), encoding="utf-8")
    blocked_agent = tmp_path / "blocked.py"
    blocked_agent.write_text(
        "print('<phase-blocked>Intervention required.</phase-blocked>')\n",
        encoding="utf-8",
    )
    assert _run_handler(retry_root, retry_plan, blocked_agent).returncode == EXIT_BLOCKED
    retry_state_path = default_state_path(retry_root, retry_plan)
    stopped_state = retry_state_path.read_bytes()

    ready_path = tmp_path / "holder-ready"
    holder = _write_blocking_agent(tmp_path, ready_path)
    sentinel_path = tmp_path / "retry-launched"
    retry_agent = tmp_path / "retry.py"
    retry_agent.write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel_path)!r}).touch()\n"
        "print('<phase-complete>Retried.</phase-complete>')\n",
        encoding="utf-8",
    )
    holder_process = _start_handler(holder_root, holder_plan, holder)

    try:
        _wait_for_readiness(holder_process, ready_path)

        retry_result = _run_handler(
            retry_root,
            retry_plan,
            retry_agent,
            extra_cli_args=("--retry-stopped",),
        )

        assert retry_result.returncode == EXIT_INVALID
        assert f"execution workspace {execution_workspace}" in retry_result.stderr
        assert retry_state_path.read_bytes() == stopped_state
        assert not sentinel_path.exists()
    finally:
        _terminate_handler(holder_process)


def test_independent_workspaces_can_execute_concurrently(tmp_path: Path) -> None:
    processes: list[subprocess.Popen[str]] = []
    ready_paths: list[Path] = []
    release_paths: list[Path] = []
    try:
        for name in ("first", "second"):
            workspace = tmp_path / name
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("# Test repository\n", encoding="utf-8")
            plan_path = workspace / "plan.md"
            plan_path.write_text(_phase(), encoding="utf-8")
            ready_path = workspace / "ready"
            release_path = workspace / "release"
            os.mkfifo(release_path)
            agent = workspace / "agent.py"
            agent.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "sys.stdin.read()\n"
                "Path(sys.argv[1]).touch()\n"
                "with Path(sys.argv[2]).open(encoding='utf-8') as release:\n"
                "    release.read(1)\n"
                "print('<phase-complete>Concurrent run complete.</phase-complete>')\n",
                encoding="utf-8",
            )
            ready_paths.append(ready_path)
            release_paths.append(release_path)
            processes.append(
                _start_handler(
                    workspace,
                    plan_path,
                    agent,
                    extra_agent_args=(str(ready_path), str(release_path)),
                )
            )

        _wait_for_all_readiness(processes, ready_paths)
        for release_path in release_paths:
            release_path.write_text("x", encoding="utf-8")

        results = [process.communicate(timeout=5) for process in processes]
        assert [process.returncode for process in processes] == [0, 0]
        assert all(stderr == "" for _, stderr in results)
    finally:
        for process in processes:
            if process.poll() is None:
                _terminate_handler(process)


def test_sigkill_attempt_requires_inspection_and_nonoverlapping_retry(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_text = _phase()
    plan_path.write_text(plan_text, encoding="utf-8")
    ready_path = tmp_path / "worker-ready"
    abandoned_agent = tmp_path / "abandoned-agent.py"
    abandoned_agent.write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "import sys\n"
        "sys.stdin.read()\n"
        f"Path({str(ready_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    completing_agent = tmp_path / "completing-agent.py"
    completing_agent.write_text(
        "print('<phase-complete>Recovered after inspection.</phase-complete>')\n",
        encoding="utf-8",
    )
    handler = _start_handler(tmp_path, plan_path, abandoned_agent)
    worker_pid: int | None = None

    try:
        _wait_for_readiness(handler, ready_path)
        state_path = default_state_path(tmp_path, plan_path)
        running = read_state(state_path)
        assert running.active_attempt is not None
        assert running.active_attempt.status is ActiveAttemptStatus.RUNNING
        assert running.active_attempt.process is not None
        worker_pid = running.active_attempt.process.pid

        handler.kill()
        assert handler.wait(timeout=3) == -signal.SIGKILL

        abandoned = read_state(state_path)
        assert abandoned.active_attempt == running.active_attempt

        plan_path.write_text(plan_text.replace("Body", "Changed body"), encoding="utf-8")
        state_before_status = state_path.read_bytes()
        status_result = subprocess.run(
            [sys.executable, "-m", "ai_session_handler", "status", "--plan", str(plan_path)],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert status_result.returncode == EXIT_INVALID
        assert "active/abandoned: phase-1" in status_result.stdout
        assert f"attempt prompt: {running.active_attempt.prompt_path}" in status_result.stdout
        assert "plan hash mismatch" in status_result.stderr
        assert state_path.read_bytes() == state_before_status
        plan_path.write_text(plan_text, encoding="utf-8")

        ordinary_result = _run_handler(tmp_path, plan_path, completing_agent)
        assert ordinary_result.returncode == EXIT_INVALID
        assert "abandoned running attempt" in ordinary_result.stderr

        overlapping_retry = _run_handler(
            tmp_path,
            plan_path,
            completing_agent,
            extra_cli_args=("--retry-stopped",),
        )
        assert overlapping_retry.returncode == EXIT_INVALID
        assert f"worker pid {worker_pid} is still alive" in overlapping_retry.stderr

        os.kill(worker_pid, signal.SIGKILL)
        _wait_for_process_exit(worker_pid)

        recovered = _run_handler(
            tmp_path,
            plan_path,
            completing_agent,
            extra_cli_args=("--retry-stopped",),
        )
        assert recovered.returncode == 0
        final_state = read_state(state_path)
        assert final_state.active_attempt is None
        assert final_state.completed_phase_ids == ("phase-1",)
    finally:
        if handler.poll() is None:
            handler.kill()
        with suppress(subprocess.TimeoutExpired):
            handler.wait(timeout=1)
        if worker_pid is not None and _process_is_running(worker_pid):
            with suppress(ProcessLookupError):
                os.kill(worker_pid, signal.SIGKILL)


def _phase(
    *,
    title: str = "One",
    number: int = 1,
    workspace: str = ".",
    body: str = "Body\n",
) -> str:
    return f"## Phase {number}: {title}\n### Workspace\n\n{workspace}\n\n### Goal\n\n{body}"


def _write_blocking_agent(directory: Path, ready_path: Path) -> Path:
    release_path = directory / f"{ready_path.name}-release"
    os.mkfifo(release_path)
    agent = directory / f"{ready_path.name}-agent.py"
    agent.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "sys.stdin.read()\n"
        f"Path({str(ready_path)!r}).touch()\n"
        f"with Path({str(release_path)!r}).open(encoding='utf-8') as release:\n"
        "    release.read(1)\n"
        "print('<phase-complete>Holder complete.</phase-complete>')\n",
        encoding="utf-8",
    )
    return agent


def _handler_argv(
    plan_path: Path,
    agent_path: Path,
    *,
    extra_agent_args: tuple[str, ...] = (),
) -> list[str]:
    command_parts = [shlex.quote(sys.executable), shlex.quote(str(agent_path))]
    command_parts.extend(shlex.quote(argument) for argument in extra_agent_args)
    return [
        sys.executable,
        "-m",
        "ai_session_handler",
        "run",
        "--plan",
        str(plan_path),
        "--agent-cmd",
        " ".join(command_parts),
        "--timeout",
        "5",
        "--quiet",
    ]


def _start_handler(
    cwd: Path,
    plan_path: Path,
    agent_path: Path,
    *,
    extra_agent_args: tuple[str, ...] = (),
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        _handler_argv(plan_path, agent_path, extra_agent_args=extra_agent_args),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _run_handler(
    cwd: Path,
    plan_path: Path,
    agent_path: Path,
    *,
    extra_cli_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_handler_argv(plan_path, agent_path), *extra_cli_args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _wait_for_readiness(process: subprocess.Popen[str], ready_path: Path) -> None:
    _wait_for_all_readiness([process], [ready_path])


def _wait_for_all_readiness(
    processes: list[subprocess.Popen[str]],
    ready_paths: list[Path],
) -> None:
    deadline = time.monotonic() + 3
    while not all(path.exists() for path in ready_paths):
        for process in processes:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    f"handler exited before readiness ({process.returncode}): "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
        if time.monotonic() >= deadline:
            pytest.fail("handler did not report readiness before the outer deadline")
        time.sleep(0.01)


def _terminate_handler(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        process.communicate(timeout=4)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=1)


def _process_is_running(process_id: int) -> bool:
    try:
        stat = Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in {"Z", "X"}


def _wait_for_process_exit(process_id: int) -> None:
    deadline = time.monotonic() + 3
    while _process_is_running(process_id):
        if time.monotonic() >= deadline:
            pytest.fail(f"worker pid {process_id} did not exit")
        time.sleep(0.01)
