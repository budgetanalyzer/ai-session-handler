"""Integration tests for process runner behavior using fake agents."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from ai_session_handler.runner import (
    EXIT_AGENT_FAILED,
    EXIT_BLOCKED,
    EXIT_NEEDS_CLARIFICATION,
    EXIT_OK,
    RunOptions,
    run_phases,
)
from ai_session_handler.state import StopReason, read_state


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


def test_run_records_timeout(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    script = _write_agent(tmp_path, "agent.py", "import time\ntime.sleep(5)\n")

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
        "## Phase 1: One\nFirst\n## Phase 2: Two\nSecond\n## Phase 3: Three\nThird\n",
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
            workspace_path=tmp_path,
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


def _options(
    tmp_path: Path,
    plan_path: Path,
    script: Path,
    *,
    timeout_seconds: float = 5,
    stop_on_regex: tuple[str, ...] = (),
) -> RunOptions:
    return RunOptions(
        workspace_path=tmp_path,
        plan_path=plan_path,
        state_path=tmp_path / ".ai-session-handler" / "plan.json",
        agent_cmd=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
        timeout_seconds=timeout_seconds,
        stop_on_regex=stop_on_regex,
    )


def _write_plan(tmp_path: Path) -> Path:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("## Phase 1: One\nImplement one.\n", encoding="utf-8")
    return plan_path


def _write_agent(tmp_path: Path, name: str, source: str) -> Path:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return script
