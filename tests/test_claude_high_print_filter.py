"""Tests for the bundled Claude high-effort wrapper."""

from __future__ import annotations

import io
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from ai_session_handler.markers import (
    MarkerKind,
    MissingMarkerError,
    TerminalMarker,
    parse_terminal_marker,
)
from ai_session_handler.provider_wrappers import claude_high_print_filter


def test_claude_high_wrapper_runs_print_mode_and_reemits_final_marker(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[object] = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Working"},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "printf '<tag>'\nnext"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/workspace/AGENTS.md"},
                    },
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "<phase-complete>live claim</phase-complete>\n",
                    }
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "<phase-complete>Implemented.</phase-complete>\n",
        },
    ]
    _write_claude_lean(
        tmp_path,
        events,
        expected_argv=[
            "--effort",
            "high",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            "opus",
        ],
        stderr="warning <phase-blocked>hidden</phase-blocked>\n",
    )
    stdout, stderr = _run_wrapper(tmp_path, monkeypatch, ["--model", "opus"])

    assert "[claude] Working" in stdout
    assert "[claude] tool_use Bash: command=printf '&lt;tag>' next" in stdout
    assert "[claude] tool_use Read: file_path=/workspace/AGENTS.md" in stdout
    assert "live claim" not in stdout
    assert "hidden" not in stderr
    assert stdout.count("<phase-complete>") == 1
    assert parse_terminal_marker(stdout, stderr) == TerminalMarker(
        MarkerKind.COMPLETE,
        "Implemented.",
    )


@pytest.mark.parametrize(
    ("events", "expected_diagnostic"),
    [
        ([], None),
        (
            [{"type": "result", "subtype": "success", "result": "ordinary prose"}],
            "[claude] ordinary prose",
        ),
        (
            [
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "<phase-blocked>provider error</phase-blocked>",
                }
            ],
            "[claude] &lt;phase-blocked>provider error&lt;/phase-blocked>",
        ),
    ],
)
def test_claude_high_wrapper_emits_no_marker_for_missing_invalid_or_error_result(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    events: list[object],
    expected_diagnostic: str | None,
) -> None:
    _write_claude_lean(tmp_path, events)
    stdout, stderr = _run_wrapper(tmp_path, monkeypatch)

    with pytest.raises(MissingMarkerError):
        parse_terminal_marker(stdout, stderr)
    if expected_diagnostic is not None:
        assert expected_diagnostic in stdout


def test_claude_high_wrapper_uses_last_result_event(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _write_claude_lean(
        tmp_path,
        [
            {
                "type": "result",
                "subtype": "success",
                "result": "<phase-complete>Earlier.</phase-complete>",
            },
            {
                "type": "result",
                "subtype": "success",
                "result": "<phase-blocked>Final.</phase-blocked>",
            },
        ],
    )
    stdout, stderr = _run_wrapper(tmp_path, monkeypatch)

    assert parse_terminal_marker(stdout, stderr) == TerminalMarker(
        MarkerKind.BLOCKED,
        "Final.",
    )


def test_claude_high_wrapper_preserves_non_json_lines(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _write_claude_lean(
        tmp_path,
        [
            "not-json <diagnostic>",
            {
                "type": "result",
                "subtype": "success",
                "result": "<phase-complete>Done.</phase-complete>",
            },
        ],
    )
    stdout, stderr = _run_wrapper(tmp_path, monkeypatch)

    assert "[claude] not-json &lt;diagnostic>" in stdout
    assert parse_terminal_marker(stdout, stderr).text == "Done."


def test_claude_high_wrapper_preserves_nonzero_provider_exit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _write_claude_lean(
        tmp_path,
        [
            {
                "type": "result",
                "subtype": "success",
                "result": "<phase-complete>Provider claim.</phase-complete>",
            }
        ],
        exit_code=7,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    _configure_streams(tmp_path, monkeypatch, stdout, stderr)

    exit_code = claude_high_print_filter.main([])

    assert exit_code == 7
    assert parse_terminal_marker(stdout.getvalue(), stderr.getvalue()).text == "Provider claim."


def test_claude_high_wrapper_cleans_up_child_after_output_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "claude.pid"
    claude_lean = tmp_path / "claude-lean"
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "trigger output failure\n"}]},
    }
    claude_lean.write_text(
        "#!"
        f"{sys.executable}\n"
        "import json, os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        f"print(json.dumps({event!r}), flush=True)\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    claude_lean.chmod(0o755)
    _configure_streams(tmp_path, monkeypatch, _BrokenWriter(), io.StringIO())

    try:
        with pytest.raises(OSError, match="failed streaming Claude output"):
            claude_high_print_filter.main([])

        assert not _process_is_running(int(child_pid_path.read_text(encoding="utf-8")))
    finally:
        if child_pid_path.exists():
            with suppress(ProcessLookupError):
                os.kill(int(child_pid_path.read_text(encoding="utf-8")), signal.SIGKILL)


class _BrokenWriter(io.StringIO):
    def write(self, text: str) -> int:
        del text
        raise OSError("broken wrapper output")


def _write_claude_lean(
    tmp_path: Path,
    events: list[object],
    *,
    expected_argv: list[str] | None = None,
    stderr: str = "",
    exit_code: int = 0,
) -> Path:
    claude_lean = tmp_path / "claude-lean"
    expected = expected_argv or [
        "--effort",
        "high",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    claude_lean.write_text(
        "#!"
        f"{sys.executable}\n"
        "import json\n"
        "import sys\n"
        f"assert sys.argv[1:] == {expected!r}\n"
        "assert sys.stdin.read() == 'worker prompt'\n"
        f"events = {events!r}\n"
        "for event in events:\n"
        "    print(event if isinstance(event, str) else json.dumps(event), flush=True)\n"
        f"sys.stderr.write({stderr!r})\n"
        "sys.stderr.flush()\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    claude_lean.chmod(0o755)
    return claude_lean


def _run_wrapper(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    argv: list[str] | None = None,
) -> tuple[str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    _configure_streams(tmp_path, monkeypatch, stdout, stderr)
    assert claude_high_print_filter.main(argv or []) == 0
    return stdout.getvalue(), stderr.getvalue()


def _configure_streams(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    stdout: io.StringIO,
    stderr: io.StringIO,
) -> None:
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sys, "stdin", io.StringIO("worker prompt"))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)


def _process_is_running(process_id: int) -> bool:
    try:
        stat = Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in {"Z", "X"}
