"""Guard the exact uv and Python pins against duplication outside their owner files."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from ai_session_handler.dev_bootstrap import read_contract

_VERSIONED_UV_PATH = re.compile(r"uv-\d+\.\d+\.\d+")
_EXCLUDED_DOC_DIRECTORIES = frozenset({"conversations"})
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _guarded_files(root: Path) -> list[Path]:
    files = [root / "README.md", root / "AGENTS.md"]
    files.extend(
        path
        for path in sorted((root / "docs").rglob("*.md"))
        if path.relative_to(root / "docs").parts[0] not in _EXCLUDED_DOC_DIRECTORIES
    )
    files.extend(sorted((root / ".github" / "workflows").glob("*.yml")))
    files.extend(sorted((root / "src").rglob("*.py")))
    return files


def pin_reference_violations(root: Path, pins: Sequence[str]) -> list[str]:
    literal = re.compile("|".join(rf"(?<![\w.]){re.escape(pin)}(?![\w.]*\d)" for pin in pins))
    violations: list[str] = []
    for path in _guarded_files(root):
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if literal.search(line) or _VERSIONED_UV_PATH.search(line):
                violations.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    return violations


def test_docs_workflows_and_scripts_do_not_repeat_pins() -> None:
    contract = read_contract(REPOSITORY_ROOT)

    violations = pin_reference_violations(
        REPOSITORY_ROOT, (contract.uv_version, contract.python_version)
    )

    assert violations == [], (
        "The uv pin must appear only in [tool.dependency-contract] in pyproject.toml and the "
        "exact Python patch only in .python-version. Refer to those owner files or to "
        ".dependency-tools/uv-<pin>/ instead:\n" + "\n".join(violations)
    )


def test_guard_reports_reintroduced_literal(tmp_path: Path) -> None:
    (tmp_path / "docs" / "conversations").mkdir(parents=True)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "README.md").write_text("ok\nuses uv 9.8.7 here\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("run .dependency-tools/uv-1.2.3/bin/uv\n", encoding="utf-8")
    (tmp_path / "docs" / "conversations" / "old.md").write_text("uv 9.8.7\n", encoding="utf-8")
    (tmp_path / "docs" / "guide.md").write_text("version 19.8.70 is fine\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("uv==9.8.7\n", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "tool.py").write_text('PIN = "9.8.7"\n', encoding="utf-8")

    violations = pin_reference_violations(tmp_path, ("9.8.7",))

    assert violations == [
        "README.md:2: uses uv 9.8.7 here",
        "AGENTS.md:1: run .dependency-tools/uv-1.2.3/bin/uv",
        ".github/workflows/ci.yml:1: uv==9.8.7",
        'src/pkg/tool.py:1: PIN = "9.8.7"',
    ]


def test_guard_reports_python_patch_pin_but_not_minor_line(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "uses the .python-version runtime (currently 3.12.99)\n", encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text(
        "supported 3.12 minor line\nrequires >=3.12,<3.13 and ==3.12.*\nnot 3.12.990\n",
        encoding="utf-8",
    )

    violations = pin_reference_violations(tmp_path, ("3.12.99",))

    assert violations == ["README.md:1: uses the .python-version runtime (currently 3.12.99)"]
