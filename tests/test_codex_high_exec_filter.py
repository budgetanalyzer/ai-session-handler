"""Tests for the bundled Codex high-reasoning wrapper."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest
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


def test_codex_high_wrapper_emits_multiline_final_result(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    _write_codex_lean(
        tmp_path,
        "<phase-complete>Implemented parser.\nAll checks pass.</phase-complete>\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sys, "stdin", io.StringIO("worker prompt"))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    exit_code = codex_high_exec_filter.main([])

    assert exit_code == 0
    assert (
        "<phase-complete>Implemented parser.\nAll checks pass.</phase-complete>"
        in stdout.getvalue()
    )


def test_codex_high_wrapper_sanitizes_invalid_final_message(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    _write_codex_lean(
        tmp_path,
        "<phase-complete>fixture result</phase-complete>\nRuntimeError: failed\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sys, "stdin", io.StringIO("worker prompt"))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    exit_code = codex_high_exec_filter.main([])

    assert exit_code == 0
    assert "<phase-complete>" not in stdout.getvalue()
    assert "[phase-complete]fixture result[/phase-complete]" in stdout.getvalue()
    assert "RuntimeError: failed" in stdout.getvalue()


def test_codex_high_wrapper_sanitizes_unclosed_live_diagnostic(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    codex_lean = tmp_path / "codex-lean"
    codex_lean.write_text(
        "#!"
        f"{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[-1]).write_text(\n"
        "    '<phase-complete>Done.</phase-complete>\\n', encoding='utf-8'\n"
        ")\n"
        "print('diagnostic quoted <phase-blocked> without a close')\n",
        encoding="utf-8",
    )
    codex_lean.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sys, "stdin", io.StringIO("worker prompt"))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    exit_code = codex_high_exec_filter.main([])

    assert exit_code == 0
    assert "diagnostic quoted [phase-blocked] without a close" in stdout.getvalue()
    assert stdout.getvalue().count("<phase-complete>") == 1


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


def test_codex_high_wrapper_cleans_up_child_after_output_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "codex.pid"
    codex_lean = tmp_path / "codex-lean"
    codex_lean.write_text(
        "#!"
        f"{sys.executable}\n"
        "import os\n"
        "from pathlib import Path\n"
        "import signal\n"
        "import time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "print('trigger broken wrapper output', flush=True)\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    codex_lean.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sys, "stdin", io.StringIO("worker prompt"))
    monkeypatch.setattr(sys, "stdout", _BrokenWriter())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    try:
        with pytest.raises(OSError, match="failed streaming Codex output"):
            codex_high_exec_filter.main([])

        assert not _process_is_running(int(child_pid_path.read_text(encoding="utf-8")))
    finally:
        if child_pid_path.exists():
            with suppress(ProcessLookupError):
                os.kill(int(child_pid_path.read_text(encoding="utf-8")), signal.SIGKILL)


def test_codex_high_wrapper_defers_startup_signal_until_child_cleanup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "codex.pid"
    codex_lean = tmp_path / "codex-lean"
    codex_lean.write_text(
        "#!"
        f"{sys.executable}\n"
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "os.kill(os.getppid(), signal.SIGTERM)\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    codex_lean.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sys, "stdin", io.StringIO("worker prompt"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    try:
        with _outer_deadline(), pytest.raises(SystemExit) as raised:
            codex_high_exec_filter.main([])

        assert raised.value.code == 128 + signal.SIGTERM
        assert not _process_is_running(int(child_pid_path.read_text(encoding="utf-8")))
        assert signal.getsignal(signal.SIGINT) is previous_sigint
        assert signal.getsignal(signal.SIGTERM) is previous_sigterm
    finally:
        if child_pid_path.exists():
            with suppress(ProcessLookupError):
                os.kill(int(child_pid_path.read_text(encoding="utf-8")), signal.SIGKILL)


def test_codex_high_wrapper_defers_repeated_signals_through_final_cleanup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _write_codex_lean(tmp_path, "<phase-complete>Done.</phase-complete>\n")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sys, "stdin", io.StringIO("worker prompt"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    original_terminate = codex_high_exec_filter._terminate_child
    original_close = codex_high_exec_filter._close_process_pipes
    closed_pipes = False

    def interrupt_cleanup(process: subprocess.Popen[str]) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGINT)
        original_terminate(process)

    def record_pipe_close(process: subprocess.Popen[str]) -> None:
        nonlocal closed_pipes
        original_close(process)
        closed_pipes = True

    monkeypatch.setattr(codex_high_exec_filter, "_terminate_child", interrupt_cleanup)
    monkeypatch.setattr(codex_high_exec_filter, "_close_process_pipes", record_pipe_close)

    with _outer_deadline(), pytest.raises(SystemExit) as raised:
        codex_high_exec_filter.main([])

    assert raised.value.code == 128 + signal.SIGTERM
    assert closed_pipes


def test_codex_high_wrapper_joins_only_threads_that_started(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "codex.pid"
    codex_lean = tmp_path / "codex-lean"
    codex_lean.write_text(
        "#!"
        f"{sys.executable}\n"
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    codex_lean.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sys, "stdin", io.StringIO("worker prompt"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    original_start = threading.Thread.start
    starts = 0

    def fail_second_start(thread: threading.Thread) -> None:
        nonlocal starts
        starts += 1
        if starts == 2:
            raise RuntimeError("thread start failed")
        _wait_for_path(child_pid_path)
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_second_start)

    try:
        with _outer_deadline(), pytest.raises(RuntimeError, match="thread start failed"):
            codex_high_exec_filter.main([])

        assert not _process_is_running(int(child_pid_path.read_text(encoding="utf-8")))
    finally:
        if child_pid_path.exists():
            with suppress(ProcessLookupError):
                os.kill(int(child_pid_path.read_text(encoding="utf-8")), signal.SIGKILL)


class _BrokenWriter(io.StringIO):
    def write(self, text: str) -> int:
        del text
        raise OSError("broken wrapper output")


def _process_is_running(process_id: int) -> bool:
    try:
        stat = Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in {"Z", "X"}


def _write_codex_lean(tmp_path: Path, final_message_literal: str) -> Path:
    codex_lean = tmp_path / "codex-lean"
    codex_lean.write_text(
        "#!"
        f"{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        f"Path(sys.argv[-1]).write_text({final_message_literal!r}, encoding='utf-8')\n",
        encoding="utf-8",
    )
    codex_lean.chmod(0o755)
    return codex_lean


def _wait_for_path(path: Path) -> None:
    deadline = time.monotonic() + 3
    while not path.exists():
        if time.monotonic() >= deadline:
            pytest.fail("wrapper child did not report readiness before the outer deadline")
        time.sleep(0.01)


@contextmanager
def _outer_deadline(seconds: float = 8.0) -> Iterator[None]:
    def expire(signal_number: int, frame: object) -> None:
        del signal_number, frame
        raise TimeoutError("subprocess test exceeded its outer deadline")

    previous_handler = signal.signal(signal.SIGALRM, expire)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
