"""Tests for the bundled Codex high-reasoning wrapper."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from ai_session_handler.provider_wrappers import codex_high_exec_filter


def test_codex_high_wrapper_filters_live_markers_and_reemits_final_marker(
    monkeypatch: MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    stdin = io.StringIO("worker prompt")
    captured_env: dict[str, str] = {}
    captured_input: list[str] = []

    def fake_run(
        args: Sequence[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        env: Mapping[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert text is True
        assert capture_output is True
        assert check is False
        assert args[:4] == ["codex-lean", "exec", "--color", "never"]
        captured_input.append(input)
        captured_env.update(env)
        final_message_path = Path(args[-1])
        final_message_path.write_text(
            "done\n<phase-complete>Implemented.</phase-complete>\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="live <phase-blocked>ignore me</phase-blocked>\n",
            stderr="err <phase-needs-clarification>ignore me</phase-needs-clarification>\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    exit_code = codex_high_exec_filter.main()

    assert exit_code == 0
    assert captured_input == ["worker prompt"]
    assert captured_env["CODEX_REASONING_EFFORT"] == "high"
    assert "<phase-blocked>" not in stdout.getvalue()
    assert "<phase-needs-clarification>" not in stderr.getvalue()
    assert "[phase-blocked]ignore me[/phase-blocked]" in stdout.getvalue()
    assert "[phase-needs-clarification]ignore me[/phase-needs-clarification]" in stderr.getvalue()
    assert stdout.getvalue().count("<phase-complete>") == 1
    assert "<phase-complete>Implemented.</phase-complete>" in stdout.getvalue()


def test_codex_high_module_entrypoint_helpfully_fails_without_codex(
    capsys: CaptureFixture[str],
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_session_handler.provider_wrappers.codex_high_exec_filter",
        ],
        input="prompt",
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": ""},
        check=False,
    )

    assert result.returncode != 0
    assert "codex-lean" in result.stderr
