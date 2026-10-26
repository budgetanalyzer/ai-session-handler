"""Tests for the frozen development-environment bootstrap."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from ai_session_handler import dev_bootstrap
from ai_session_handler.dev_bootstrap import (
    BootstrapError,
    DependencyContract,
    bootstrap_environment,
    ensure_pinned_uv,
    frozen_sync_command,
    lock_check_command,
    pinned_uv_executable,
    read_contract,
    sanitized_environment,
)

CONTRACT = DependencyContract(uv_version="9.8.7", python_version="3.12.99")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _write_contract(root: Path, uv: str = '"9.8.7"', python: str = "3.12.99\n") -> None:
    (root / "pyproject.toml").write_text(
        f"[tool.dependency-contract]\n# renovate: datasource=pypi depName=uv\nuv = {uv}\n",
        encoding="utf-8",
    )
    (root / ".python-version").write_text(python, encoding="utf-8")


class FakeRunner:
    """Record commands and simulate venv creation, uv installation, and uv subcommands."""

    def __init__(
        self,
        *,
        reported_version: str = "9.8.7",
        failing: str | None = None,
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.reported_version = reported_version
        self.failing = failing

    def __call__(self, command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        args = tuple(command)
        self.commands.append(args)
        if self.failing is not None and self.failing in args:
            return subprocess.CompletedProcess(args, 1, "", f"{self.failing} failed")
        if args[1:3] == ("-m", "venv"):
            (Path(args[-1]) / "bin").mkdir(parents=True, exist_ok=True)
        elif args[1:4] == ("-m", "pip", "install"):
            (Path(args[0]).parent / "uv").write_text("", encoding="utf-8")
        elif args[1:] == ("--version",):
            return subprocess.CompletedProcess(
                args, 0, f"uv {self.reported_version} (abc 2026-10-01)\n", ""
            )
        return subprocess.CompletedProcess(args, 0, "", "")


def test_repository_contract_is_exact() -> None:
    contract = read_contract(REPOSITORY_ROOT)

    assert dev_bootstrap._UV_PIN_PATTERN.fullmatch(contract.uv_version)
    assert contract.python_version.startswith("3.12.")


def test_repository_runtime_dependencies_stay_empty() -> None:
    import tomllib

    pyproject = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["dependencies"] == []
    assert pyproject["project"]["requires-python"] == ">=3.12,<3.13"


def test_locked_third_party_packages_come_from_pypi() -> None:
    import tomllib

    lock = tomllib.loads((REPOSITORY_ROOT / "uv.lock").read_text(encoding="utf-8"))
    sources = {package["name"]: package["source"] for package in lock["package"]}

    assert sources.pop("ai-session-handler") == {"editable": "."}
    assert sources
    assert all(source == {"registry": dev_bootstrap.PYPI_INDEX} for source in sources.values())


def test_read_contract_parses_pins(tmp_path: Path) -> None:
    _write_contract(tmp_path)

    assert read_contract(tmp_path) == CONTRACT


@pytest.mark.parametrize("uv", ['">=9.8"', '"9.8"', "987", '"9.8.7rc1"'])
def test_read_contract_rejects_inexact_uv_pin(tmp_path: Path, uv: str) -> None:
    _write_contract(tmp_path, uv=uv)

    with pytest.raises(BootstrapError, match=r"tool\.dependency-contract\.uv must be an exact"):
        read_contract(tmp_path)


def test_read_contract_rejects_missing_uv_pin(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / ".python-version").write_text("3.12.99\n", encoding="utf-8")

    with pytest.raises(BootstrapError, match="got None"):
        read_contract(tmp_path)


@pytest.mark.parametrize("python", ["3.12\n", "3.13.1\n", ">=3.12\n", ""])
def test_read_contract_rejects_inexact_python_pin(tmp_path: Path, python: str) -> None:
    _write_contract(tmp_path, python=python)

    with pytest.raises(BootstrapError, match=r"\.python-version must contain"):
        read_contract(tmp_path)


def test_read_contract_reports_malformed_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool\n", encoding="utf-8")

    with pytest.raises(BootstrapError, match=r"could not read .*pyproject\.toml"):
        read_contract(tmp_path)


def test_pinned_uv_executable_is_versioned(tmp_path: Path) -> None:
    assert pinned_uv_executable(tmp_path, CONTRACT) == (
        tmp_path / ".dependency-tools" / "uv-9.8.7" / "bin" / "uv"
    )


def test_ensure_pinned_uv_installs_isolated_binary_only_pin(tmp_path: Path) -> None:
    runner = FakeRunner()

    uv = ensure_pinned_uv(tmp_path, CONTRACT, Path("/usr/bin/python3"), runner)

    tool_root = tmp_path / ".dependency-tools" / "uv-9.8.7"
    assert uv == tool_root / "bin" / "uv"
    assert runner.commands == [
        ("/usr/bin/python3", "-m", "venv", "--clear", str(tool_root)),
        (
            str(tool_root / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "--isolated",
            "--disable-pip-version-check",
            "--index-url",
            "https://pypi.org/simple",
            "--no-deps",
            "--only-binary=:all:",
            "uv==9.8.7",
        ),
        (str(uv), "--version"),
    ]


def test_ensure_pinned_uv_reuses_matching_install(tmp_path: Path) -> None:
    uv = pinned_uv_executable(tmp_path, CONTRACT)
    uv.parent.mkdir(parents=True)
    uv.write_text("", encoding="utf-8")
    runner = FakeRunner()

    assert ensure_pinned_uv(tmp_path, CONTRACT, Path("/usr/bin/python3"), runner) == uv
    assert runner.commands == [(str(uv), "--version")]


def test_ensure_pinned_uv_rejects_version_mismatch(tmp_path: Path) -> None:
    runner = FakeRunner(reported_version="9.8.6")

    with pytest.raises(BootstrapError, match=r"does not report the pinned version uv 9\.8\.7"):
        ensure_pinned_uv(tmp_path, CONTRACT, Path("/usr/bin/python3"), runner)


def test_ensure_pinned_uv_reports_failed_installation(tmp_path: Path) -> None:
    runner = FakeRunner(failing="uv==9.8.7")

    with pytest.raises(BootstrapError, match=r"could not install uv==9\.8\.7 .*failed"):
        ensure_pinned_uv(tmp_path, CONTRACT, Path("/usr/bin/python3"), runner)


def test_lock_and_sync_commands_are_frozen() -> None:
    uv = Path("/tools/uv")

    assert lock_check_command(uv, CONTRACT) == (
        "/tools/uv",
        "lock",
        "--check",
        "--python",
        "3.12.99",
    )
    assert frozen_sync_command(uv, CONTRACT) == (
        "/tools/uv",
        "sync",
        "--frozen",
        "--extra",
        "dev",
        "--group",
        "build",
        "--python",
        "3.12.99",
    )


def test_bootstrap_checks_lock_before_frozen_sync(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    runner = FakeRunner()

    assert bootstrap_environment(tmp_path, Path("/usr/bin/python3"), runner) == CONTRACT

    uv = pinned_uv_executable(tmp_path, CONTRACT)
    assert runner.commands[-2:] == [
        lock_check_command(uv, CONTRACT),
        frozen_sync_command(uv, CONTRACT),
    ]


def test_bootstrap_stops_on_stale_lock_without_sync(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    runner = FakeRunner(failing="lock")

    with pytest.raises(BootstrapError, match=r"uv\.lock is missing, stale, or invalid"):
        bootstrap_environment(tmp_path, Path("/usr/bin/python3"), runner)

    assert not any("sync" in command for command in runner.commands)


def test_bootstrap_reports_failed_sync(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    runner = FakeRunner(failing="sync")

    with pytest.raises(
        BootstrapError, match=r"frozen synchronization of \.venv from uv\.lock failed"
    ):
        bootstrap_environment(tmp_path, Path("/usr/bin/python3"), runner)


def test_main_reports_bootstrap_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(root: Path, bootstrap_python: Path) -> DependencyContract:
        raise BootstrapError("no interpreter")

    monkeypatch.setattr(dev_bootstrap, "bootstrap_environment", fail)

    assert dev_bootstrap.main([]) == 1
    assert capsys.readouterr().err == "dev bootstrap failed: no interpreter\n"


def test_main_rejects_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert dev_bootstrap.main(["--upgrade"]) == 2
    assert "does not accept arguments" in capsys.readouterr().err


def test_sanitized_environment_removes_index_and_python_overrides() -> None:
    environment = sanitized_environment(
        {
            "PATH": "/usr/bin",
            "PIP_INDEX_URL": "https://mirror.invalid/simple",
            "UV_INDEX_URL": "https://mirror.invalid/simple",
            "UV_PYTHON": "3.13",
            "UV_PROJECT_ENVIRONMENT": "/tmp/other",
            "VIRTUAL_ENV": "/tmp/active",
        }
    )

    assert environment == {"PATH": "/usr/bin"}
