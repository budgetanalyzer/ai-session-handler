"""Integration tests for process runner behavior using fake agents."""

from __future__ import annotations

import io
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import UUID

import pytest

from ai_session_handler.config import default_state_path
from ai_session_handler.phases import PlanParseError, read_plan_snapshot
from ai_session_handler.runner import (
    EXIT_AGENT_FAILED,
    EXIT_BLOCKED,
    EXIT_NEEDS_CLARIFICATION,
    EXIT_OK,
    CommandTemplateError,
    RunOptions,
    create_run_id,
    render_command_template,
    run_agent_process,
    run_phases,
)
from ai_session_handler.state import (
    ActiveAttemptError,
    ActiveAttemptStatus,
    AttemptStatus,
    PlanHashMismatchError,
    RunnerState,
    StopReason,
    read_state,
    write_state,
)
from ai_session_handler.transcripts import transcript_path


@pytest.fixture(autouse=True)
def _plan_repository_instructions(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# Test repository\n", encoding="utf-8")


def test_run_records_stdout_complete_marker(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "import sys\n"
        "prompt = sys.stdin.read()\n"
        "assert 'selected_phase_id: phase-1' in prompt\n"
        "print('<phase-complete>Implemented phase one.</phase-complete>')\n",
    )

    outcome = run_phases(_options(tmp_path, plan_path, script))

    state = read_state(tmp_path / ".ai-session-handler" / "plan.json")
    assert outcome.exit_code == EXIT_OK
    assert state.completed_phase_ids == ("phase-1",)
    assert state.stop is None
    assert state.last_run is not None
    assert state.last_run.summary == "Implemented phase one."
    assert Path(state.last_run.transcript_path).exists()


def test_run_ids_are_unique_for_attempts_in_the_same_second(tmp_path: Path) -> None:
    phase = read_plan_snapshot(_write_plan(tmp_path)).phases[0]
    timestamp = datetime(2026, 7, 5, 12, 1, 2, tzinfo=UTC)

    first = create_run_id(phase, timestamp=timestamp)
    second = create_run_id(phase, timestamp=timestamp)

    assert first != second
    prefix = "20260705T120102Z-phase-1-"
    assert first.startswith(prefix)
    UUID(first.removeprefix(prefix))
    UUID(second.removeprefix(prefix))


def test_existing_transcript_for_forced_duplicate_id_prevents_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = _write_plan(tmp_path)
    sentinel_path = tmp_path / "launched"
    script = _write_agent(
        tmp_path,
        "agent.py",
        "from pathlib import Path\n"
        f"Path({str(sentinel_path)!r}).touch()\n"
        "print('<phase-complete>Unexpected.</phase-complete>')\n",
    )
    options = _options(tmp_path, plan_path, script)
    forced_id = "forced-duplicate-attempt"
    existing_transcript = transcript_path(options.state_path.parent, forced_id)
    existing_transcript.parent.mkdir(parents=True)
    existing_transcript.write_text("original transcript\n", encoding="utf-8")
    monkeypatch.setattr("ai_session_handler.runner.create_run_id", lambda phase: forced_id)

    outcome = run_phases(options)

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.EXECUTION_IO_FAILED
    assert not sentinel_path.exists()
    assert existing_transcript.read_text(encoding="utf-8") == "original transcript\n"


def test_prepared_attempt_is_durable_before_worker_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(tmp_path, "agent.py", "print('unused')\n")
    options = _options(tmp_path, plan_path, script)

    def refuse_launch(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        prepared = read_state(options.state_path)
        assert prepared.active_attempt is not None
        assert prepared.active_attempt.status is ActiveAttemptStatus.PREPARED
        assert prepared.active_attempt.process is None
        raise FileNotFoundError("missing executable")

    monkeypatch.setattr("ai_session_handler.runner.subprocess.Popen", refuse_launch)

    outcome = run_phases(options)

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.active_attempt is None
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.LAUNCH_FAILED
    assert outcome.state.last_run is not None
    assert outcome.state.last_run.status is AttemptStatus.LAUNCH_FAILED


def test_outcome_write_failure_preserves_durable_running_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "print('<phase-complete>Finished before state failure.</phase-complete>')\n",
    )
    options = _options(tmp_path, plan_path, script)
    writes = 0

    def fail_third_write(path: Path, state: RunnerState) -> None:
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("state replacement failed")
        write_state(path, state)

    monkeypatch.setattr("ai_session_handler.runner.write_state", fail_third_write)

    outcome = run_phases(options)

    durable = read_state(options.state_path)
    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert "could not record attempt outcome" in outcome.message
    assert durable.active_attempt is not None
    assert durable.active_attempt.status is ActiveAttemptStatus.RUNNING
    assert durable.stop is None
    assert durable.completed_phase_ids == ()
    with pytest.raises(ActiveAttemptError):
        run_phases(options)


def test_run_records_stderr_blocked_marker(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "import sys\nprint('<phase-blocked>Need access.</phase-blocked>', file=sys.stderr)\n",
    )

    outcome = run_phases(_options(tmp_path, plan_path, script))

    state = outcome.state
    assert outcome.exit_code == EXIT_BLOCKED
    assert state.stop is not None
    assert state.stop.reason is StopReason.BLOCKED
    assert state.stop.message == "Need access."


def test_run_records_needs_clarification_marker(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "print('<phase-needs-clarification>Pick schema?</phase-needs-clarification>')\n",
    )

    outcome = run_phases(_options(tmp_path, plan_path, script))

    assert outcome.exit_code == EXIT_NEEDS_CLARIFICATION
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.NEEDS_CLARIFICATION
    assert outcome.state.stop.clarification_request == "Pick schema?"


def test_run_records_nonzero_process_failure(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(tmp_path, "agent.py", "import sys\nsys.exit(7)\n")

    outcome = run_phases(_options(tmp_path, plan_path, script))

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.AGENT_FAILED
    assert outcome.state.stop.message == "agent command exited with code 7"
    assert outcome.state.last_run is not None
    transcript = Path(outcome.state.last_run.transcript_path).read_text(encoding="utf-8")
    assert f"plan_workspace_path: {tmp_path}" in transcript
    assert f"execution_workspace_path: {tmp_path}" in transcript
    assert "argv:" in transcript
    assert "[runner] process exited with code 7 without stdout/stderr output" in transcript


def test_run_records_timeout(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(tmp_path, "agent.py", "import time\ntime.sleep(5)\n")

    outcome = run_phases(_options(tmp_path, plan_path, script, timeout_seconds=0.1))

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.TIMEOUT


def test_timeout_is_enforced_when_agent_does_not_read_large_stdin(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "## Phase 1: One\n### Workspace\n\n.\n\n### Goal\n\n" + ("large body\n" * 200_000),
        encoding="utf-8",
    )
    script = _write_agent(
        tmp_path,
        "agent.py",
        "import time\nprint('agent started', flush=True)\ntime.sleep(5)\n",
    )

    outcome = run_phases(_options(tmp_path, plan_path, script, timeout_seconds=0.1))

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.TIMEOUT


def test_run_records_stop_regex(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "import time\nprint('context limit approaching', flush=True)\ntime.sleep(5)\n",
    )

    outcome = run_phases(
        _options(tmp_path, plan_path, script, timeout_seconds=2, stop_on_regex=("context limit",))
    )

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.STOP_REGEX


def test_stop_regex_kills_resistant_descendants_after_leader_exit(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    child_pid_path = tmp_path / "child.pid"
    grandchild_pid_path = tmp_path / "grandchild.pid"
    script = _write_process_tree_agent(
        tmp_path,
        child_pid_path=child_pid_path,
        grandchild_pid_path=grandchild_pid_path,
        leader_source="print('stop now', flush=True)\n",
    )

    try:
        outcome = run_phases(
            _options(
                tmp_path,
                plan_path,
                script,
                timeout_seconds=3,
                stop_on_regex=("stop now",),
            )
        )

        assert outcome.exit_code == EXIT_AGENT_FAILED
        assert outcome.state.stop is not None
        assert outcome.state.stop.reason is StopReason.STOP_REGEX
        assert not _process_is_running(_read_pid(child_pid_path))
        assert not _process_is_running(_read_pid(grandchild_pid_path))
    finally:
        _kill_recorded_processes(child_pid_path, grandchild_pid_path)


def test_timeout_kills_wrapper_child_and_resistant_grandchild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = _write_plan(tmp_path)
    child_pid_path = tmp_path / "codex.pid"
    grandchild_pid_path = tmp_path / "grandchild.pid"
    grandchild_source = (
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(grandchild_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    codex_lean = _write_agent(
        tmp_path,
        "codex-lean",
        f"#!{sys.executable}\n"
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        f"subprocess.Popen([{sys.executable!r}, '-c', {grandchild_source!r}])\n"
        "while not Path(sys.argv[-1]).parent.joinpath('never-created').exists():\n"
        "    time.sleep(1)\n",
    )
    codex_lean.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    wrapper_command = (
        f"{shlex.quote(sys.executable)} -m "
        "ai_session_handler.provider_wrappers.codex_high_exec_filter"
    )
    options = replace(
        _options(tmp_path, plan_path, codex_lean, timeout_seconds=1),
        agent_cmd=wrapper_command,
    )

    try:
        outcome = run_phases(options)

        assert outcome.exit_code == EXIT_AGENT_FAILED
        assert outcome.state.stop is not None
        assert outcome.state.stop.reason is StopReason.TIMEOUT
        assert not _process_is_running(_read_pid(child_pid_path))
        assert not _process_is_running(_read_pid(grandchild_pid_path))
    finally:
        _kill_recorded_processes(child_pid_path, grandchild_pid_path)


def test_broken_live_output_sink_cleans_up_worker_group(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    phase = read_plan_snapshot(plan_path).phases[0]
    child_pid_path = tmp_path / "child.pid"
    grandchild_pid_path = tmp_path / "grandchild.pid"
    script = _write_process_tree_agent(
        tmp_path,
        child_pid_path=child_pid_path,
        grandchild_pid_path=grandchild_pid_path,
        leader_source="print('trigger broken sink', flush=True)\nwhile True:\n    time.sleep(1)\n",
    )

    try:
        with pytest.raises(OSError, match="broken output sink"):
            run_agent_process(
                agent_cmd=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
                prompt_text="prompt",
                prompt_path=tmp_path / "prompt.txt",
                plan_workspace_path=tmp_path,
                execution_workspace_path=tmp_path,
                run_id="broken-output",
                transcript_file=tmp_path / "transcript.txt",
                state_file=tmp_path / "state.json",
                phase=phase,
                plan_path=plan_path,
                timeout_seconds=3,
                stop_patterns=(),
                stdout=_BrokenWriter(),
            )

        assert not _process_is_running(_read_pid(child_pid_path))
        assert not _process_is_running(_read_pid(grandchild_pid_path))
    finally:
        _kill_recorded_processes(child_pid_path, grandchild_pid_path)


def test_transcript_header_failure_prevents_worker_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = _write_plan(tmp_path)
    phase = read_plan_snapshot(plan_path).phases[0]
    sentinel_path = tmp_path / "launched"
    script = _write_agent(
        tmp_path,
        "agent.py",
        f"from pathlib import Path\nPath({str(sentinel_path)!r}).touch()\n",
    )
    monkeypatch.setattr(
        "ai_session_handler.runner.open_text_exclusively",
        lambda path: _BrokenWriter(),
    )

    with pytest.raises(OSError, match="broken output sink"):
        run_agent_process(
            agent_cmd=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
            prompt_text="prompt",
            prompt_path=tmp_path / "prompt.txt",
            plan_workspace_path=tmp_path,
            execution_workspace_path=tmp_path,
            run_id="broken-transcript",
            transcript_file=tmp_path / "transcript.txt",
            state_file=tmp_path / "state.json",
            phase=phase,
            plan_path=plan_path,
            timeout_seconds=3,
            stop_patterns=(),
        )

    assert not sentinel_path.exists()


def test_transcript_write_failure_cleans_up_worker_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_path = _write_plan(tmp_path)
    phase = read_plan_snapshot(plan_path).phases[0]
    child_pid_path = tmp_path / "child.pid"
    grandchild_pid_path = tmp_path / "grandchild.pid"
    script = _write_process_tree_agent(
        tmp_path,
        child_pid_path=child_pid_path,
        grandchild_pid_path=grandchild_pid_path,
        leader_source=(
            "print('trigger transcript failure', flush=True)\nwhile True:\n    time.sleep(1)\n"
        ),
    )
    monkeypatch.setattr(
        "ai_session_handler.runner.open_text_exclusively",
        lambda path: _HeaderOnlyWriter(),
    )

    try:
        with pytest.raises(OSError, match="broken transcript"):
            run_agent_process(
                agent_cmd=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
                prompt_text="prompt",
                prompt_path=tmp_path / "prompt.txt",
                plan_workspace_path=tmp_path,
                execution_workspace_path=tmp_path,
                run_id="broken-transcript-body",
                transcript_file=tmp_path / "transcript.txt",
                state_file=tmp_path / "state.json",
                phase=phase,
                plan_path=plan_path,
                timeout_seconds=3,
                stop_patterns=(),
            )

        assert not _process_is_running(_read_pid(child_pid_path))
        assert not _process_is_running(_read_pid(grandchild_pid_path))
    finally:
        _kill_recorded_processes(child_pid_path, grandchild_pid_path)


def test_process_signal_handlers_are_restored_after_success(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "print('<phase-complete>Done.</phase-complete>')\n",
    )

    outcome = run_phases(_options(tmp_path, plan_path, script))

    assert outcome.exit_code == EXIT_OK
    assert signal.getsignal(signal.SIGINT) is previous_sigint
    assert signal.getsignal(signal.SIGTERM) is previous_sigterm


@pytest.mark.parametrize("interrupt_signal", [signal.SIGINT, signal.SIGTERM])
def test_catchable_signal_records_interruption_and_cleans_up_worker_group(
    tmp_path: Path,
    interrupt_signal: signal.Signals,
) -> None:
    plan_path = _write_plan(tmp_path)
    child_pid_path = tmp_path / "child.pid"
    grandchild_pid_path = tmp_path / "grandchild.pid"
    script = _write_process_tree_agent(
        tmp_path,
        child_pid_path=child_pid_path,
        grandchild_pid_path=grandchild_pid_path,
        leader_source="while True:\n    time.sleep(1)\n",
    )
    handler = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ai_session_handler",
            "run",
            "--plan",
            str(plan_path),
            "--agent-cmd",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
            "--timeout",
            "10",
            "--quiet",
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    try:
        _wait_for_paths(handler, child_pid_path, grandchild_pid_path)
        handler.send_signal(interrupt_signal)

        expected_return_code = (
            -signal.SIGINT if interrupt_signal is signal.SIGINT else 128 + signal.SIGTERM
        )
        assert handler.wait(timeout=4) == expected_return_code
        assert not _process_is_running(_read_pid(child_pid_path))
        assert not _process_is_running(_read_pid(grandchild_pid_path))
        state = read_state(default_state_path(tmp_path, plan_path))
        assert state.active_attempt is None
        assert state.stop is not None
        assert state.stop.reason is StopReason.INTERRUPTED
        assert state.last_run is not None
        assert state.last_run.status is AttemptStatus.INTERRUPTED
    finally:
        if handler.poll() is None:
            handler.kill()
        with suppress(subprocess.TimeoutExpired):
            handler.wait(timeout=1)
        _kill_recorded_processes(child_pid_path, grandchild_pid_path)


def test_invalid_stop_regex_does_not_write_state(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(tmp_path, "agent.py", "print('unused')\n")
    state_path = tmp_path / ".ai-session-handler" / "plan.json"

    with pytest.raises(ValueError, match="invalid stop regex"):
        run_phases(_options(tmp_path, plan_path, script, stop_on_regex=("[",)))

    assert not state_path.exists()


@pytest.mark.parametrize("timeout_seconds", [0.0, -1.0, float("nan")])
def test_invalid_timeout_does_not_write_state(
    tmp_path: Path,
    timeout_seconds: float,
) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(tmp_path, "agent.py", "print('unused')\n")
    state_path = tmp_path / ".ai-session-handler" / "plan.json"

    with pytest.raises(ValueError, match="finite number greater than 0"):
        run_phases(_options(tmp_path, plan_path, script, timeout_seconds=timeout_seconds))

    assert not state_path.exists()


def test_render_command_template_preserves_placeholder_paths_with_spaces() -> None:
    command = render_command_template(
        "agent --prompt {prompt_file} --cwd={workspace} --run {run_id}",
        prompt_file=Path("/tmp/my workspace/prompt file.txt"),
        workspace=Path("/tmp/my workspace"),
        run_id="run-1",
        transcript_file=Path("/tmp/my workspace/transcript.txt"),
        state_file=Path("/tmp/my workspace/state.json"),
    )

    assert command == [
        "agent",
        "--prompt",
        "/tmp/my workspace/prompt file.txt",
        "--cwd=/tmp/my workspace",
        "--run",
        "run-1",
    ]


def test_render_command_template_preserves_quotes_inside_placeholder_values() -> None:
    command = render_command_template(
        "agent --prompt={prompt_file}",
        prompt_file=Path('/tmp/my "quoted" workspace/prompt file.txt'),
        workspace=Path('/tmp/my "quoted" workspace'),
        run_id="run-1",
        transcript_file=Path('/tmp/my "quoted" workspace/transcript.txt'),
        state_file=Path('/tmp/my "quoted" workspace/state.json'),
    )

    assert command == ["agent", '--prompt=/tmp/my "quoted" workspace/prompt file.txt']


def test_render_command_template_preserves_literal_escaped_braces() -> None:
    command = render_command_template(
        'agent \'--payload={{"status": "ok"}}\'',
        prompt_file=Path("/tmp/prompt.txt"),
        workspace=Path("/tmp/workspace"),
        run_id="run-1",
        transcript_file=Path("/tmp/transcript.txt"),
        state_file=Path("/tmp/state.json"),
    )

    assert command == ["agent", '--payload={"status": "ok"}']


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("", "empty command"),
        ("   ", "empty command"),
        ("''", "empty command"),
        ("agent {}", "positional command placeholder is not allowed: {}"),
        ("agent {0}", "positional command placeholder is not allowed: {0}"),
        ("agent {workspace.name}", "attribute and index access are not allowed"),
        ("agent {workspace[0]}", "attribute and index access are not allowed"),
        ("agent {workspace!r}", "conversion is not allowed"),
        ("agent {workspace:}", "format specification is not allowed"),
        ("agent {workspace:>20}", "format specification is not allowed"),
        ("agent {workspace:{run_id}}", "format specification is not allowed"),
        ("agent {unknown}", "unsupported command placeholder: {unknown}"),
        ("agent {workspace", "invalid command template syntax"),
        ('agent "unterminated', "invalid command template quoting"),
    ],
)
def test_render_command_template_rejects_invalid_syntax(template: str, message: str) -> None:
    with pytest.raises(CommandTemplateError, match=re.escape(message)):
        render_command_template(
            template,
            prompt_file=Path("/tmp/prompt.txt"),
            workspace=Path("/tmp/workspace"),
            run_id="run-1",
            transcript_file=Path("/tmp/transcript.txt"),
            state_file=Path("/tmp/state.json"),
        )


def test_invalid_command_template_does_not_launch_or_mutate_state(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    sentinel_path = tmp_path / "launched"
    script = _write_agent(
        tmp_path,
        "agent.py",
        "from pathlib import Path\n"
        f"Path({str(sentinel_path)!r}).touch()\n"
        "print('<phase-complete>Unexpected.</phase-complete>')\n",
    )
    state_path = tmp_path / ".ai-session-handler" / "plan.json"
    state_path.parent.mkdir()
    original_state = b'{"existing": true}\n'
    state_path.write_bytes(original_state)
    options = _options(tmp_path, plan_path, script)

    with pytest.raises(CommandTemplateError, match="positional command placeholder"):
        run_phases(replace(options, agent_cmd=f"{options.agent_cmd} {{}}"))

    assert not sentinel_path.exists()
    assert state_path.read_bytes() == original_state
    assert not (state_path.parent / "prompts").exists()
    assert not (state_path.parent / "transcripts").exists()


def test_run_preserves_prompt_file_placeholder_with_workspace_spaces(tmp_path: Path) -> None:
    workspace = tmp_path / "my workspace"
    workspace.mkdir()
    plan_path = _write_plan(workspace)
    record_path = workspace / "argv.txt"
    script = _write_agent(
        workspace,
        "agent.py",
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')\n"
        "print('<phase-complete>Read prompt path.</phase-complete>')\n",
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} "
    command += f"{shlex.quote(str(record_path))} {{prompt_file}}"

    outcome = run_phases(
        RunOptions(
            plan_workspace_path=workspace,
            plan_path=plan_path,
            state_path=workspace / ".ai-session-handler" / "plan.json",
            agent_cmd=command,
            timeout_seconds=5,
        )
    )

    prompt_path = Path(record_path.read_text(encoding="utf-8"))
    assert outcome.exit_code == EXIT_OK
    assert prompt_path.exists()
    assert prompt_path.parent == workspace / ".ai-session-handler" / "prompts"


def test_run_handles_large_output_before_marker(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "for index in range(2500):\n"
        "    print(f'line {index}')\n"
        "print('<phase-complete>Large output done.</phase-complete>')\n",
    )

    outcome = run_phases(_options(tmp_path, plan_path, script))

    assert outcome.exit_code == EXIT_OK
    assert outcome.state.completed_phase_ids == ("phase-1",)


def test_run_records_missing_marker(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(tmp_path, "agent.py", "print('no marker here')\n")

    outcome = run_phases(_options(tmp_path, plan_path, script))

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.MISSING_MARKER


def test_run_records_multiple_markers(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "print('<phase-complete>One</phase-complete>')\n"
        "print('<phase-complete>Two</phase-complete>')\n",
    )

    outcome = run_phases(_options(tmp_path, plan_path, script))

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.MULTIPLE_MARKERS


def test_logged_fixture_followed_by_failure_prose_does_not_complete(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "print('<phase-complete>fixture result</phase-complete>')\n"
        "print('RuntimeError: implementation failed')\n",
    )

    outcome = run_phases(_options(tmp_path, plan_path, script))

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.completed_phase_ids == ()
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.INVALID_MARKER


def test_fenced_result_like_example_does_not_complete(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "print('```text')\nprint('<phase-complete>fixture result</phase-complete>')\n",
    )

    outcome = run_phases(_options(tmp_path, plan_path, script))

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.completed_phase_ids == ()
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.INVALID_MARKER


def test_result_on_both_streams_is_rejected(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "import sys\n"
        "print('<phase-complete>stdout result</phase-complete>')\n"
        "print('<phase-blocked>stderr result</phase-blocked>', file=sys.stderr)\n",
    )

    outcome = run_phases(_options(tmp_path, plan_path, script))

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.completed_phase_ids == ()
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.MULTIPLE_MARKERS


def test_stderr_diagnostics_do_not_affect_final_stdout_result(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "import sys\n"
        "print('diagnostic without marker tags', file=sys.stderr)\n"
        "print('<phase-complete>Implemented.</phase-complete>')\n",
    )

    outcome = run_phases(_options(tmp_path, plan_path, script))

    assert outcome.exit_code == EXIT_OK
    assert outcome.state.completed_phase_ids == ("phase-1",)


def test_nonzero_exit_overrides_complete_marker(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "import sys\nprint('<phase-complete>Claimed completion.</phase-complete>')\nsys.exit(7)\n",
    )

    outcome = run_phases(_options(tmp_path, plan_path, script))

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.completed_phase_ids == ()
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.AGENT_FAILED


def test_timeout_overrides_complete_marker(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "import time\n"
        "print('<phase-complete>Claimed completion.</phase-complete>', flush=True)\n"
        "time.sleep(5)\n",
    )

    outcome = run_phases(_options(tmp_path, plan_path, script, timeout_seconds=0.1))

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.completed_phase_ids == ()
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.TIMEOUT


def test_stop_regex_overrides_complete_marker(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "agent.py",
        "import time\n"
        "print('<phase-complete>Claimed completion.</phase-complete>', flush=True)\n"
        "time.sleep(5)\n",
    )

    outcome = run_phases(
        _options(
            tmp_path,
            plan_path,
            script,
            timeout_seconds=2,
            stop_on_regex=("Claimed completion",),
        )
    )

    assert outcome.exit_code == EXIT_AGENT_FAILED
    assert outcome.state.completed_phase_ids == ()
    assert outcome.state.stop is not None
    assert outcome.state.stop.reason is StopReason.STOP_REGEX


def test_max_phases_runs_two_fresh_processes(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "## Phase 1: One\n### Workspace\n\n.\n\n### Goal\n\nFirst\n"
        "## Phase 2: Two\n### Workspace\n\n.\n\n### Goal\n\nSecond\n"
        "## Phase 3: Three\n### Workspace\n\n.\n\n### Goal\n\nThird\n",
        encoding="utf-8",
    )
    record_path = tmp_path / "runs.txt"
    script = _write_agent(
        tmp_path,
        "agent.py",
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).open('a', encoding='utf-8').write(sys.argv[2] + '\\n')\n"
        "print('<phase-complete>Done.</phase-complete>')\n",
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} "
    command += f"{shlex.quote(str(record_path))} {{run_id}}"

    outcome = run_phases(
        RunOptions(
            plan_workspace_path=tmp_path,
            plan_path=plan_path,
            state_path=tmp_path / ".ai-session-handler" / "plan.json",
            agent_cmd=command,
            max_phases=2,
            timeout_seconds=5,
        )
    )

    state = outcome.state
    assert outcome.exit_code == EXIT_OK
    assert state.completed_phase_ids == ("phase-1", "phase-2")
    assert record_path.read_text(encoding="utf-8").count("\n") == 2


def test_plan_edit_after_phase_stops_before_next_worker(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "## Phase 1: One\n### Workspace\n.\n### Goal\nFirst original.\n"
        "## Phase 2: Two\n### Workspace\n.\n### Goal\nSecond original.\n",
        encoding="utf-8",
    )
    record_path = tmp_path / "runs.txt"
    script = _write_agent(
        tmp_path,
        "edit-plan.py",
        "from pathlib import Path\n"
        "import sys\n"
        "prompt = sys.stdin.read()\n"
        "plan_path = Path(sys.argv[1])\n"
        "record_path = Path(sys.argv[2])\n"
        "with record_path.open('a', encoding='utf-8') as record:\n"
        "    record.write(('phase-1' if 'selected_phase_id: phase-1' in prompt "
        "else 'phase-2') + '\\n')\n"
        "if 'selected_phase_id: phase-1' in prompt:\n"
        "    text = plan_path.read_text(encoding='utf-8')\n"
        "    plan_path.write_text(text.replace('Second original.', 'Second edited.'), "
        "encoding='utf-8')\n"
        "print('<phase-complete>Completed snapshot phase.</phase-complete>')\n",
    )
    command = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} "
        f"{shlex.quote(str(plan_path))} {shlex.quote(str(record_path))}"
    )
    options = RunOptions(
        plan_workspace_path=tmp_path,
        plan_path=plan_path,
        state_path=tmp_path / ".ai-session-handler" / "plan.json",
        agent_cmd=command,
        timeout_seconds=5,
    )

    with pytest.raises(PlanHashMismatchError):
        run_phases(options)

    state = read_state(options.state_path)
    assert state.completed_phase_ids == ("phase-1",)
    assert state.last_run is not None
    assert state.last_run.summary == "Completed snapshot phase."
    assert record_path.read_text(encoding="utf-8").splitlines() == ["phase-1"]


def test_plan_edit_during_last_phase_prevents_runner_complete(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(
        tmp_path,
        "edit-last-plan.py",
        "from pathlib import Path\n"
        "import sys\n"
        "sys.stdin.read()\n"
        "plan_path = Path(sys.argv[1])\n"
        "plan_path.write_text(plan_path.read_text(encoding='utf-8') + '\\nEdited.\\n', "
        "encoding='utf-8')\n"
        "print('<phase-complete>Last phase snapshot complete.</phase-complete>')\n",
    )
    options = RunOptions(
        plan_workspace_path=tmp_path,
        plan_path=plan_path,
        state_path=tmp_path / ".ai-session-handler" / "plan.json",
        agent_cmd=(
            f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} "
            f"{shlex.quote(str(plan_path))}"
        ),
        timeout_seconds=5,
    )

    with pytest.raises(PlanHashMismatchError):
        run_phases(options)

    state = read_state(options.state_path)
    assert state.completed_phase_ids == ("phase-1",)
    assert state.last_run is not None
    assert state.last_run.summary == "Last phase snapshot complete."


def test_consecutive_phases_use_distinct_workspaces_and_plan_owned_artifacts(
    tmp_path: Path,
) -> None:
    plan_workspace = tmp_path / "plan-repo"
    first_workspace = tmp_path / "first-service"
    second_workspace = tmp_path / "second-service"
    for workspace in (plan_workspace, first_workspace, second_workspace):
        workspace.mkdir()
        (workspace / "AGENTS.md").write_text(f"# {workspace.name}\n", encoding="utf-8")

    plan_path = plan_workspace / "docs" / "plans" / "cross-repo.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "## Phase 1: First service\n"
        "### Workspace\n\n../first-service\n\n"
        "### Goal\n\nImplement first.\n"
        "## Phase 2: Second service\n"
        "### Workspace\n\n../second-service\n\n"
        "### Goal\n\nImplement second.\n",
        encoding="utf-8",
    )
    record_path = plan_workspace / "worker-records.jsonl"
    script = _write_agent(
        plan_workspace,
        "agent.py",
        "import os\n"
        "from pathlib import Path\n"
        "import sys\n"
        "sys.stdin.read()\n"
        "with Path(sys.argv[1]).open('a', encoding='utf-8') as output:\n"
        "    output.write(os.getcwd() + '\\t' + sys.argv[2] + '\\n')\n"
        "print('<phase-complete>Done.</phase-complete>')\n",
    )
    command = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} "
        f"{shlex.quote(str(record_path))} {{workspace}}"
    )
    generated_dir = plan_workspace / ".ai-session-handler"

    outcome = run_phases(
        RunOptions(
            plan_workspace_path=plan_workspace,
            plan_path=plan_path,
            state_path=generated_dir / "cross-repo.json",
            agent_cmd=command,
            timeout_seconds=5,
        )
    )

    records = [line.split("\t") for line in record_path.read_text(encoding="utf-8").splitlines()]
    assert outcome.exit_code == EXIT_OK
    assert [record[0] for record in records] == [
        str(first_workspace),
        str(second_workspace),
    ]
    assert [record[1] for record in records] == [
        str(first_workspace),
        str(second_workspace),
    ]

    assert (generated_dir / "cross-repo.json").is_file()
    prompts = sorted((generated_dir / "prompts").glob("*.txt"))
    transcripts = sorted((generated_dir / "transcripts").glob("*.txt"))
    assert len(prompts) == 2
    assert len(transcripts) == 2
    for prompt, execution_workspace in zip(
        prompts,
        (first_workspace, second_workspace),
        strict=True,
    ):
        prompt_text = prompt.read_text(encoding="utf-8")
        assert f"plan_workspace_path: {plan_workspace}" in prompt_text
        assert f"execution_workspace_path: {execution_workspace}" in prompt_text
        assert f"state_path: {generated_dir / 'cross-repo.json'}" in prompt_text
    assert f"execution_workspace_path: {first_workspace}" in transcripts[0].read_text(
        encoding="utf-8"
    )
    assert f"execution_workspace_path: {second_workspace}" in transcripts[1].read_text(
        encoding="utf-8"
    )
    assert not (first_workspace / ".ai-session-handler").exists()
    assert not (second_workspace / ".ai-session-handler").exists()


def test_invalid_workspace_fails_before_worker_launch(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "## Phase 1: Missing\n### Workspace\n../missing\n### Goal\nImplement.\n",
        encoding="utf-8",
    )
    sentinel_path = tmp_path / "launched"
    script = _write_agent(
        tmp_path,
        "agent.py",
        "from pathlib import Path\n"
        f"Path({str(sentinel_path)!r}).touch()\n"
        "print('<phase-complete>Unexpected.</phase-complete>')\n",
    )

    with pytest.raises(PlanParseError, match="not an existing directory"):
        run_phases(_options(tmp_path, plan_path, script))

    assert not sentinel_path.exists()
    assert not (tmp_path / ".ai-session-handler" / "plan.json").exists()


def test_retry_stopped_phase_uses_same_execution_workspace(tmp_path: Path) -> None:
    plan_workspace = tmp_path / "plan-repo"
    execution_workspace = tmp_path / "service"
    for workspace in (plan_workspace, execution_workspace):
        workspace.mkdir()
        (workspace / "AGENTS.md").write_text("# Instructions\n", encoding="utf-8")
    plan_path = plan_workspace / "plan.md"
    plan_path.write_text(
        "## Phase 1: Service\n### Workspace\n../service\n### Goal\nImplement.\n",
        encoding="utf-8",
    )
    record_path = plan_workspace / "working-directories.txt"
    script = _write_agent(
        plan_workspace,
        "agent.py",
        "import os\n"
        "from pathlib import Path\n"
        "import sys\n"
        "record_path = Path(sys.argv[1])\n"
        "previous = record_path.read_text(encoding='utf-8') if record_path.exists() else ''\n"
        "record_path.write_text(previous + os.getcwd() + '\\n', encoding='utf-8')\n"
        "if previous:\n"
        "    print('<phase-complete>Retried.</phase-complete>')\n"
        "else:\n"
        "    print('<phase-blocked>Intervention required.</phase-blocked>')\n",
    )
    options = RunOptions(
        plan_workspace_path=plan_workspace,
        plan_path=plan_path,
        state_path=plan_workspace / ".ai-session-handler" / "plan.json",
        agent_cmd=(
            f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} "
            f"{shlex.quote(str(record_path))}"
        ),
        timeout_seconds=5,
    )

    first_outcome = run_phases(options)
    retry_outcome = run_phases(replace(options, retry_stopped=True))

    assert first_outcome.exit_code == EXIT_BLOCKED
    assert retry_outcome.exit_code == EXIT_OK
    assert record_path.read_text(encoding="utf-8").splitlines() == [
        str(execution_workspace),
        str(execution_workspace),
    ]


def _options(
    tmp_path: Path,
    plan_path: Path,
    script: Path,
    *,
    timeout_seconds: float = 5,
    stop_on_regex: tuple[str, ...] = (),
) -> RunOptions:
    return RunOptions(
        plan_workspace_path=tmp_path,
        plan_path=plan_path,
        state_path=tmp_path / ".ai-session-handler" / "plan.json",
        agent_cmd=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
        timeout_seconds=timeout_seconds,
        stop_on_regex=stop_on_regex,
    )


def _write_plan(tmp_path: Path) -> Path:
    (tmp_path / "AGENTS.md").write_text("# Test repository\n", encoding="utf-8")
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "## Phase 1: One\n### Workspace\n\n.\n\n### Goal\n\nImplement one.\n",
        encoding="utf-8",
    )
    return plan_path


def _write_agent(tmp_path: Path, name: str, source: str) -> Path:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return script


class _BrokenWriter(io.StringIO):
    def write(self, text: str) -> int:
        del text
        raise OSError("broken output sink")


class _HeaderOnlyWriter(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.write_count = 0

    def write(self, text: str) -> int:
        self.write_count += 1
        if self.write_count > 1:
            raise OSError("broken transcript")
        return super().write(text)


def _write_process_tree_agent(
    tmp_path: Path,
    *,
    child_pid_path: Path,
    grandchild_pid_path: Path,
    leader_source: str,
) -> Path:
    grandchild_source = (
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(grandchild_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    child_source = (
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild_source!r}])\n"
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    source = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"subprocess.Popen([sys.executable, '-c', {child_source!r}])\n"
        f"while not (Path({str(child_pid_path)!r}).exists() "
        f"and Path({str(grandchild_pid_path)!r}).exists()):\n"
        "    time.sleep(0.01)\n"
        f"{leader_source}"
    )
    return _write_agent(tmp_path, "process-tree-agent.py", source)


def _read_pid(path: Path) -> int:
    return int(path.read_text(encoding="utf-8"))


def _process_is_running(process_id: int) -> bool:
    try:
        stat = Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in {"Z", "X"}


def _kill_recorded_processes(*paths: Path) -> None:
    for path in paths:
        if not path.exists():
            continue
        with suppress(ProcessLookupError):
            os.kill(_read_pid(path), signal.SIGKILL)


def _wait_for_paths(process: subprocess.Popen[str], *paths: Path) -> None:
    deadline = time.monotonic() + 3
    while not all(path.exists() for path in paths):
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                f"handler exited before worker readiness ({process.returncode}): "
                f"stdout={stdout!r}, stderr={stderr!r}"
            )
        if time.monotonic() >= deadline:
            pytest.fail("worker tree did not report readiness before the outer deadline")
        time.sleep(0.01)
