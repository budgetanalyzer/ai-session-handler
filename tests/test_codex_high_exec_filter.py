"""Tests for the bundled Codex high-reasoning wrapper."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from ai_session_handler.provider_wrappers import codex_high_exec_filter


def test_codex_high_wrapper_filters_live_markers_and_reemits_final_marker(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    stdin = io.StringIO("worker prompt")
    codex_lean = tmp_path / "codex-lean"
    codex_lean.write_text(
        "#!"
        f"{sys.executable}\n"
        "from pathlib import Path\n"
        "import os\n"
        "import sys\n"
        "assert Path(sys.argv[0]).name == 'codex-lean'\n"
        "assert sys.argv[1:4] == ['exec', '--color', 'never']\n"
        "assert os.environ['CODEX_REASONING_EFFORT'] == 'high'\n"
        "assert sys.stdin.read() == 'worker prompt'\n"
        "final_message_path = Path(sys.argv[-1])\n"
        "final_message_path.write_text(\n"
        "    'done\\n<phase-complete>Implemented.</phase-complete>\\n',\n"
        "    encoding='utf-8',\n"
        ")\n"
        "print('live <phase-blocked>ignore me</phase-blocked>', flush=True)\n"
        "print(\n"
        "    'err <phase-needs-clarification>ignore me</phase-needs-clarification>',\n"
        "    file=sys.stderr,\n"
        "    flush=True,\n"
        ")\n",
        encoding="utf-8",
    )
    codex_lean.chmod(0o755)

    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    exit_code = codex_high_exec_filter.main([])

    assert exit_code == 0
    assert "<phase-blocked>" not in stdout.getvalue()
    assert "<phase-needs-clarification>" not in stderr.getvalue()
    assert "live " in stdout.getvalue()
    assert "err " in stderr.getvalue()
    assert "ignore me" not in stdout.getvalue()
    assert "ignore me" not in stderr.getvalue()
    assert stdout.getvalue().count("<phase-complete>") == 1
    assert "<phase-complete>Implemented.</phase-complete>" in stdout.getvalue()


def test_codex_high_wrapper_accepts_model_option(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    stdin = io.StringIO("worker prompt")
    codex_lean = tmp_path / "codex-lean"
    codex_lean.write_text(
        "#!"
        f"{sys.executable}\n"
        "from pathlib import Path\n"
        "import os\n"
        "import sys\n"
        "assert os.environ['CODEX_MODEL'] == 'gpt-5.5'\n"
        "final_message_path = Path(sys.argv[-1])\n"
        "final_message_path.write_text(\n"
        "    '<phase-complete>Implemented.</phase-complete>\\n',\n"
        "    encoding='utf-8',\n"
        ")\n",
        encoding="utf-8",
    )
    codex_lean.chmod(0o755)

    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    exit_code = codex_high_exec_filter.main(["--model", "gpt-5.5"])

    assert exit_code == 0
    assert stderr.getvalue() == ""
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
