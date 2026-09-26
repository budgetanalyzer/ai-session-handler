"""Installed-entrypoint acceptance tests for the integrated runner workflow."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Final

import pytest
from pytest import MonkeyPatch

from ai_session_handler.config import default_state_path, plan_generated_path
from ai_session_handler.outcomes import read_outcome
from ai_session_handler.runner import (
    EXIT_AGENT_FAILED,
    EXIT_BLOCKED,
    EXIT_INVALID,
    EXIT_NEEDS_CLARIFICATION,
)
from ai_session_handler.state import AttemptStatus, read_state

_HANDLER: Final[Path] = Path(sys.executable).with_name("ai-session-handler")
_CODEX_WRAPPER: Final[Path] = Path(sys.executable).with_name("ai-session-handler-codex-high")


@pytest.fixture(autouse=True)
def _repository_instructions(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# Test repository\n", encoding="utf-8")


def test_installed_console_scripts_expose_help() -> None:
    handler = subprocess.run(
        [_HANDLER, "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    wrapper = subprocess.run(
        [_CODEX_WRAPPER, "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert handler.returncode == 0
    assert "Run a provider-agnostic AI agent plan to completion." in handler.stdout
    assert handler.stderr == ""
    assert wrapper.returncode == 0
    assert "Run codex-lean exec with high reasoning" in wrapper.stdout
    assert wrapper.stderr == ""


def test_installed_entrypoint_preserves_handoffs_across_clarification_and_blocked_retries(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "docs" / "plans" / "workflow.md"
    plan_path.parent.mkdir(parents=True)
    initial_preamble = "# Integrated workflow\n\nUse the selected result field.\n\n"
    phases = "".join(
        _phase(number=number, title=title)
        for number, title in ((1, "Prepare"), (2, "Decide"), (3, "Finish"))
    )
    plan_path.write_text(initial_preamble + phases, encoding="utf-8")
    config_path = tmp_path / ".ai-session-handler" / "config.json"
    assert not config_path.exists()
    phase_three_ready = tmp_path / "phase-three-ready"
    worker_path = tmp_path / "fake-worker.py"
    worker_path.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "prompt = sys.stdin.read()\n"
        "if 'selected_phase_id: phase-1' in prompt:\n"
        "    print('<phase-complete>Prepared durable input.</phase-complete>')\n"
        "elif 'selected_phase_id: phase-2' in prompt:\n"
        "    if 'Clarification: result field is result_kind.' in prompt:\n"
        "        assert 'phase-1 | phase-complete |' in prompt\n"
        "        print('<phase-complete>Used result_kind.</phase-complete>')\n"
        "    else:\n"
        "        print('<phase-needs-clarification>Which result field?"
        "</phase-needs-clarification>')\n"
        "elif Path('phase-three-ready').exists():\n"
        "    assert prompt.count('Create phase-three-ready.') == 1\n"
        "    assert prompt.count('Keep prior outcomes discoverable.') == 1\n"
        "    assert 'phase-1 | phase-complete |' in prompt\n"
        "    assert 'phase-2 | needs-clarification |' in prompt\n"
        "    assert 'phase-2 | phase-complete |' in prompt\n"
        "    assert 'phase-3 | blocked |' not in prompt\n"
        "    print('<phase-complete>Integrated workflow complete.</phase-complete>')\n"
        "else:\n"
        "    print('<phase-blocked>Create phase-three-ready.\\n'\n"
        "          'Keep prior outcomes discoverable.</phase-blocked>')\n",
        encoding="utf-8",
    )
    agent_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(worker_path))}"

    clarification = _run_handler(tmp_path, plan_path, agent_cmd)

    assert clarification.returncode == EXIT_NEEDS_CLARIFICATION
    assert "Which result field?" in clarification.stdout
    state_path = default_state_path(tmp_path, plan_path)
    state_after_clarification = read_state(state_path)
    assert state_after_clarification.completed_phase_ids == ("phase-1",)
    assert [outcome.status for outcome in state_after_clarification.committed_outcomes] == [
        AttemptStatus.PHASE_COMPLETE,
        AttemptStatus.NEEDS_CLARIFICATION,
    ]

    clarified_preamble = initial_preamble + "Clarification: result field is result_kind.\n\n"
    plan_path.write_text(clarified_preamble + phases, encoding="utf-8")
    state_before_acceptance = state_path.read_bytes()

    unaccepted = _run_handler(
        tmp_path,
        plan_path,
        agent_cmd,
        extra_args=("--retry-stopped",),
    )

    assert unaccepted.returncode == EXIT_INVALID
    assert "plan hash mismatch" in unaccepted.stderr
    assert state_path.read_bytes() == state_before_acceptance

    blocked = _run_handler(
        tmp_path,
        plan_path,
        agent_cmd,
        extra_args=("--retry-stopped", "--accept-plan-change"),
    )

    assert blocked.returncode == EXIT_BLOCKED
    assert "Create phase-three-ready." in blocked.stdout
    state_after_block = read_state(state_path)
    assert state_after_block.completed_phase_ids == ("phase-1", "phase-2")

    refused = _run_handler(tmp_path, plan_path, agent_cmd)

    assert refused.returncode == EXIT_INVALID
    assert "phase phase-3 is stopped: blocked" in refused.stderr

    phase_three_ready.touch()
    completed = _run_handler(
        tmp_path,
        plan_path,
        agent_cmd,
        extra_args=("--retry-stopped",),
    )

    assert completed.returncode == 0
    assert completed.stdout == "runner-complete: all phases complete\n"
    assert completed.stderr == ""
    assert not config_path.exists()
    final_state = read_state(state_path)
    assert final_state.completed_phase_ids == ("phase-1", "phase-2", "phase-3")
    assert [outcome.status for outcome in final_state.committed_outcomes] == [
        AttemptStatus.PHASE_COMPLETE,
        AttemptStatus.NEEDS_CLARIFICATION,
        AttemptStatus.PHASE_COMPLETE,
        AttemptStatus.BLOCKED,
        AttemptStatus.PHASE_COMPLETE,
    ]

    attempts = {outcome.attempt_id for outcome in final_state.committed_outcomes}
    assert len(attempts) == 5
    for reference in final_state.committed_outcomes:
        record = read_outcome(Path(reference.path))
        assert record.attempt_id == reference.attempt_id
        assert record.status is reference.status
        assert Path(record.artifacts.prompt_path).is_file()
        assert Path(record.artifacts.transcript_path).is_file()

    generated_path = plan_generated_path(tmp_path, plan_path)
    prompts = sorted((generated_path / "prompts").glob("*.txt"))
    transcripts = sorted((generated_path / "transcripts").glob("*.txt"))
    outcomes = sorted((generated_path / "outcomes").glob("*.json"))
    assert len(prompts) == len(transcripts) == len(outcomes) == 5
    assert any(
        "Clarification: result field is result_kind." in prompt.read_text(encoding="utf-8")
        and "selected_phase_id: phase-2" in prompt.read_text(encoding="utf-8")
        for prompt in prompts
    )
    final_retry_prompt = next(
        prompt.read_text(encoding="utf-8")
        for prompt in prompts
        if "Keep prior outcomes discoverable." in prompt.read_text(encoding="utf-8")
    )
    assert final_retry_prompt.count("Create phase-three-ready.") == 1
    assert final_retry_prompt.count("Keep prior outcomes discoverable.") == 1
    for reference in final_state.committed_outcomes[:3]:
        assert (
            f"{reference.phase_id} | {reference.status.value} | {reference.path}"
            in final_retry_prompt
        )
    assert (
        final_state.committed_outcomes[3].path
        not in final_retry_prompt.split("earlier_committed_outcomes:", 1)[1]
    )


def test_installed_entrypoint_keeps_identical_plan_paths_separate(tmp_path: Path) -> None:
    first_plan = tmp_path / "docs" / "first" / "plan.md"
    second_plan = tmp_path / "docs" / "second" / "plan.md"
    first_plan.parent.mkdir(parents=True)
    second_plan.parent.mkdir(parents=True)
    plan_text = _phase(number=1, title="Same content")
    first_plan.write_text(plan_text, encoding="utf-8")
    second_plan.write_text(plan_text, encoding="utf-8")
    worker_path = tmp_path / "complete.py"
    worker_path.write_text(
        "print('<phase-complete>Distinct plan complete.</phase-complete>')\n",
        encoding="utf-8",
    )
    agent_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(worker_path))}"

    first = _run_handler(tmp_path, first_plan, agent_cmd)
    second = _run_handler(tmp_path, second_plan, agent_cmd)

    assert first.returncode == second.returncode == 0
    first_generated = plan_generated_path(tmp_path, first_plan)
    second_generated = plan_generated_path(tmp_path, second_plan)
    assert first_generated != second_generated
    assert len(list((first_generated / "outcomes").glob("*.json"))) == 1
    assert len(list((second_generated / "outcomes").glob("*.json"))) == 1


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (lambda state: state.update({"schema_version": 2}), "unexpected key schema_version"),
        (lambda state: state.pop("active_attempt"), "missing required key active_attempt"),
        (
            lambda state: state.update(
                {
                    "plan": {
                        "path": "/tmp/plan.md",
                        "sha256": "0" * 64,
                        "accepted_at": "2025-01-01T00:00:00Z",
                        "version": 2,
                    }
                }
            ),
            "unexpected key plan.version",
        ),
    ],
)
def test_installed_status_rejects_malformed_current_state_without_rewriting_it(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], object],
    expected_error: str,
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(number=1, title="Malformed state"), encoding="utf-8")
    state_path = default_state_path(tmp_path, plan_path)
    state_path.parent.mkdir(parents=True)
    state: dict[str, object] = {
        "plan": None,
        "completed_phase_ids": [],
        "committed_outcomes": [],
        "current_phase": None,
        "active_attempt": None,
        "stop": None,
        "last_run": None,
    }
    mutate(state)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    original = state_path.read_bytes()

    result = subprocess.run(
        [_HANDLER, "status", "--plan", plan_path],
        check=False,
        capture_output=True,
        cwd=tmp_path,
        text=True,
        timeout=5,
    )

    assert result.returncode == EXIT_INVALID
    assert expected_error in result.stderr
    assert state_path.read_bytes() == original


def test_installed_entrypoint_rejects_misleading_marker_output(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(number=1, title="Marker framing"), encoding="utf-8")
    worker_path = tmp_path / "misleading.py"
    worker_path.write_text(
        "print('fixture: <phase-complete>not a result</phase-complete>')\n"
        "print('the operation actually failed')\n",
        encoding="utf-8",
    )
    agent_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(worker_path))}"

    result = _run_handler(tmp_path, plan_path, agent_cmd)

    assert result.returncode == EXIT_AGENT_FAILED
    assert "invalid-marker" in result.stderr
    state = read_state(default_state_path(tmp_path, plan_path))
    assert state.completed_phase_ids == ()
    assert state.last_run is not None
    assert state.last_run.status is AttemptStatus.INVALID_MARKER
    transcript = Path(state.last_run.transcript_path).read_text(encoding="utf-8")
    assert "the operation actually failed" in transcript


def test_installed_handler_accepts_normalized_codex_wrapper_diagnostics(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(number=1, title="Wrapper diagnostics"), encoding="utf-8")
    _write_fake_codex_lean(
        tmp_path,
        final_message="<phase-complete>Authoritative result.</phase-complete>\n",
        stdout_diagnostic="review: <phase-blocked > is malformed\r\n",
        stderr_diagnostic="```python\nprint('unfinished fence')",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    result = _run_handler(tmp_path, plan_path, shlex.quote(str(_CODEX_WRAPPER)))

    assert result.returncode == 0
    assert result.stdout == "runner-complete: all phases complete\n"
    assert result.stderr == ""
    state = read_state(default_state_path(tmp_path, plan_path))
    assert state.completed_phase_ids == ("phase-1",)
    assert state.last_run is not None
    transcript = Path(state.last_run.transcript_path).read_text(encoding="utf-8")
    assert "[codex] review: &lt;phase-blocked > is malformed" in transcript
    assert "[codex] ```python" in transcript
    assert "[codex] print('unfinished fence')" in transcript
    assert "<phase-complete>Authoritative result.</phase-complete>" in transcript


def test_installed_handler_prefers_nonzero_wrapper_exit_over_valid_result(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(number=1, title="Wrapper failure"), encoding="utf-8")
    _write_fake_codex_lean(
        tmp_path,
        final_message="<phase-complete>Unaccepted claim.</phase-complete>\n",
        exit_code=9,
    )
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    result = _run_handler(tmp_path, plan_path, shlex.quote(str(_CODEX_WRAPPER)))

    assert result.returncode == EXIT_AGENT_FAILED
    assert "agent command exited with code 9" in result.stderr
    state = read_state(default_state_path(tmp_path, plan_path))
    assert state.completed_phase_ids == ()
    assert state.last_run is not None
    assert state.last_run.status is AttemptStatus.AGENT_FAILED


def test_installed_entrypoint_timeout_cleans_up_resistant_descendant(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_phase(number=1, title="Process cleanup"), encoding="utf-8")
    descendant_pid_path = tmp_path / "descendant.pid"
    descendant_path = tmp_path / "descendant.py"
    descendant_path.write_text(
        "import os\n"
        "import signal\n"
        "import time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(descendant_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    worker_path = tmp_path / "worker-with-descendant.py"
    worker_path.write_text(
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        f"subprocess.Popen([sys.executable, {str(descendant_path)!r}])\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    agent_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(worker_path))}"
    descendant_pid: int | None = None

    try:
        result = _run_handler(
            tmp_path,
            plan_path,
            agent_cmd,
            extra_args=("--timeout", "1"),
        )

        assert result.returncode == EXIT_AGENT_FAILED
        assert "timeout" in result.stderr
        descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
        _wait_for_process_exit(descendant_pid)
    finally:
        if descendant_pid is None and descendant_pid_path.exists():
            with suppress(OSError, ValueError):
                descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
        if descendant_pid is not None and _process_is_running(descendant_pid):
            with suppress(ProcessLookupError):
                os.kill(descendant_pid, signal.SIGKILL)


def _phase(*, number: int, title: str) -> str:
    return f"## Phase {number}: {title}\n### Workspace\n\n.\n\n### Goal\n\nExercise {title}.\n\n"


def _run_handler(
    cwd: Path,
    plan_path: Path,
    agent_cmd: str,
    *,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _HANDLER,
            "run",
            "--plan",
            plan_path,
            "--agent-cmd",
            agent_cmd,
            "--quiet",
            *extra_args,
        ],
        check=False,
        capture_output=True,
        cwd=cwd,
        text=True,
        timeout=10,
    )


def _write_fake_codex_lean(
    tmp_path: Path,
    *,
    final_message: str,
    stdout_diagnostic: str = "",
    stderr_diagnostic: str = "",
    exit_code: int = 0,
) -> Path:
    codex_lean = tmp_path / "codex-lean"
    codex_lean.write_text(
        "#!"
        f"{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        f"Path(sys.argv[-1]).write_text({final_message!r}, encoding='utf-8')\n"
        f"sys.stdout.write({stdout_diagnostic!r})\n"
        "sys.stdout.flush()\n"
        f"sys.stderr.write({stderr_diagnostic!r})\n"
        "sys.stderr.flush()\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    codex_lean.chmod(0o755)
    return codex_lean


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
            pytest.fail(f"worker descendant pid {process_id} did not exit")
        time.sleep(0.01)
