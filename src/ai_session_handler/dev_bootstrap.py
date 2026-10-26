"""Build the frozen development environment from the checked-in ``uv.lock``.

Run from the repository root with ``PYTHONPATH=src python3 -m ai_session_handler.dev_bootstrap``.
The module uses only the standard library so it can run before ``.venv`` exists.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PYPI_INDEX: Final = "https://pypi.org/simple"
DEPENDENCY_TOOLS_DIR: Final = ".dependency-tools"
_UV_PIN_PATTERN: Final = re.compile(r"^\d+\.\d+\.\d+$")
_PYTHON_PIN_PATTERN: Final = re.compile(r"^3\.12\.\d+$")
_OVERRIDE_VARIABLES: Final = (
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
    "PIP_INDEX_URL",
    "UV_BUILD_CONSTRAINT",
    "UV_CONSTRAINT",
    "UV_DEFAULT_INDEX",
    "UV_EXTRA_INDEX_URL",
    "UV_FIND_LINKS",
    "UV_INDEX",
    "UV_INDEX_URL",
    "UV_MANAGED_PYTHON",
    "UV_NO_DEFAULT_GROUPS",
    "UV_NO_DEV",
    "UV_NO_GROUP",
    "UV_NO_INSTALL_PROJECT",
    "UV_NO_MANAGED_PYTHON",
    "UV_OVERRIDE",
    "UV_PROJECT_ENVIRONMENT",
    "UV_PYTHON",
    "VIRTUAL_ENV",
)

RunCommand = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class DependencyContract:
    uv_version: str
    python_version: str


class BootstrapError(RuntimeError):
    """A user-actionable failure while preparing the frozen development environment."""


def read_contract(root: Path) -> DependencyContract:
    """Read the exact uv pin from ``pyproject.toml`` and the Python pin from ``.python-version``."""
    pyproject_path = root / "pyproject.toml"
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BootstrapError(f"could not read {pyproject_path}: {exc}") from exc
    uv_version = pyproject.get("tool", {}).get("dependency-contract", {}).get("uv")
    if not isinstance(uv_version, str) or not _UV_PIN_PATTERN.fullmatch(uv_version):
        raise BootstrapError(
            f"{pyproject_path}: tool.dependency-contract.uv must be an exact X.Y.Z version, "
            f"got {uv_version!r}"
        )

    python_version_path = root / ".python-version"
    try:
        python_version = python_version_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BootstrapError(f"could not read {python_version_path}: {exc}") from exc
    if not _PYTHON_PIN_PATTERN.fullmatch(python_version):
        raise BootstrapError(
            f"{python_version_path} must contain one exact Python 3.12 patch version, "
            f"got {python_version!r}"
        )
    return DependencyContract(uv_version=uv_version, python_version=python_version)


def pinned_uv_executable(root: Path, contract: DependencyContract) -> Path:
    """Return the versioned repository-local path of the pinned uv binary."""
    return root / DEPENDENCY_TOOLS_DIR / f"uv-{contract.uv_version}" / "bin" / "uv"


def uv_install_command(tool_python: Path, contract: DependencyContract) -> tuple[str, ...]:
    return (
        str(tool_python),
        "-m",
        "pip",
        "install",
        "--isolated",
        "--disable-pip-version-check",
        "--index-url",
        PYPI_INDEX,
        "--no-deps",
        "--only-binary=:all:",
        f"uv=={contract.uv_version}",
    )


def lock_check_command(uv_executable: Path, contract: DependencyContract) -> tuple[str, ...]:
    """Return the non-mutating manifest-versus-lock freshness check."""
    return (str(uv_executable), "lock", "--check", "--python", contract.python_version)


def frozen_sync_command(uv_executable: Path, contract: DependencyContract) -> tuple[str, ...]:
    """Return the frozen synchronization of the complete build and development environment."""
    return (
        str(uv_executable),
        "sync",
        "--frozen",
        "--extra",
        "dev",
        "--group",
        "build",
        "--python",
        contract.python_version,
    )


def sanitized_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the environment without inherited index or uv Python-selection overrides."""
    environment = dict(os.environ if environ is None else environ)
    for name in _OVERRIDE_VARIABLES:
        environment.pop(name, None)
    return environment


def run_command(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=sanitized_environment(),
        check=False,
        capture_output=True,
        text=True,
    )


def ensure_pinned_uv(
    root: Path,
    contract: DependencyContract,
    bootstrap_python: Path,
    run: RunCommand = run_command,
) -> Path:
    """Install the pinned uv into its versioned tool directory and verify its version."""
    uv_executable = pinned_uv_executable(root, contract)
    if _uv_version_matches(uv_executable, root, contract, run):
        return uv_executable

    tool_root = uv_executable.parent.parent
    _run_checked(
        run,
        (str(bootstrap_python), "-m", "venv", "--clear", str(tool_root)),
        root,
        f"could not create the isolated uv environment {tool_root}",
    )
    _run_checked(
        run,
        uv_install_command(tool_root / "bin" / "python", contract),
        root,
        f"could not install uv=={contract.uv_version} from {PYPI_INDEX}",
    )
    if not _uv_version_matches(uv_executable, root, contract, run):
        raise BootstrapError(
            f"{uv_executable} does not report the pinned version uv {contract.uv_version}"
        )
    return uv_executable


def bootstrap_environment(
    root: Path,
    bootstrap_python: Path,
    run: RunCommand = run_command,
) -> DependencyContract:
    """Prove ``uv.lock`` current and synchronize ``.venv`` from it without refreshing the lock."""
    contract = read_contract(root)
    uv_executable = ensure_pinned_uv(root, contract, bootstrap_python, run)
    _run_checked(
        run,
        lock_check_command(uv_executable, contract),
        root,
        "uv.lock is missing, stale, or invalid; refresh it only as an intentional lock update",
    )
    _run_checked(
        run,
        frozen_sync_command(uv_executable, contract),
        root,
        "frozen synchronization of .venv from uv.lock failed",
    )
    return contract


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        print("dev bootstrap does not accept arguments", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[2]
    try:
        contract = bootstrap_environment(root, Path(sys.executable))
    except BootstrapError as exc:
        print(f"dev bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Synchronized .venv from uv.lock with uv {contract.uv_version} "
        f"and Python {contract.python_version}."
    )
    return 0


def _uv_version_matches(
    uv_executable: Path, root: Path, contract: DependencyContract, run: RunCommand
) -> bool:
    if not uv_executable.is_file():
        return False
    try:
        result = run((str(uv_executable), "--version"), root)
    except OSError:
        return False
    reported = result.stdout.strip().split(" (")[0]
    return result.returncode == 0 and reported == f"uv {contract.uv_version}"


def _run_checked(run: RunCommand, command: Sequence[str], root: Path, message: str) -> None:
    try:
        result = run(command, root)
    except OSError as exc:
        raise BootstrapError(f"{message}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise BootstrapError(f"{message}: {detail}")


if __name__ == "__main__":
    raise SystemExit(main())
