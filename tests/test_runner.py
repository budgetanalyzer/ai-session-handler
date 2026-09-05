"""Integration tests for process runner behavior using fake agents."""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ai_session_handler.phases import PlanParseError
from ai_session_handler.runner import (
    EXIT_AGENT_FAILED,
    EXIT_BLOCKED,
    EXIT_NEEDS_CLARIFICATION,
    EXIT_OK,
    CommandTemplateError,
    RunOptions,
    render_command_template,
    run_phases,
)
from ai_session_handler.state import StopReason, read_state


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
