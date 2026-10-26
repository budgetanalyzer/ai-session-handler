"""Guard the Build workflow's pinned Actions and frozen dependency setup."""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
BUILD_WORKFLOW = WORKFLOW_DIRECTORY / "build.yml"

_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
_PINNED_REF = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
_RELEASE_COMMENT = re.compile(r"^\s+# v\d+(?:\.\d+)*\s*$")


def action_ref_violations(text: str) -> list[str]:
    violations: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _USES.match(line)
        if match is None or match["ref"].startswith("./"):
            continue
        if not _PINNED_REF.match(match["ref"]) or not _RELEASE_COMMENT.match(match["rest"]):
            violations.append(f"{number}: {line.strip()}")
    return violations


def test_external_actions_are_full_sha_pinned_with_release_comments() -> None:
    for workflow in sorted(WORKFLOW_DIRECTORY.glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        assert action_ref_violations(text) == [], workflow.name


def test_action_ref_guard_reports_floating_and_uncommented_refs() -> None:
    text = (
        "      - uses: actions/checkout@v7\n"
        "      - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0\n"
        "        uses: actions/cache@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v4\n"
        "      - uses: ./local-action\n"
    )

    assert action_ref_violations(text) == [
        "1: - uses: actions/checkout@v7",
        "2: - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0",
    ]


def test_build_workflow_uses_frozen_repository_bootstrap() -> None:
    text = BUILD_WORKFLOW.read_text(encoding="utf-8")

    assert "python-version-file: .python-version" in text
    assert "python-version:" not in text
    assert "cache:" not in text
    assert "pip install" not in text
    assert not re.search(r"\buv\s+(?:lock|sync|pip|tool|run)\b", text)
    assert "run: python -m ai_session_handler.dev_bootstrap" in text

    quality_commands = [
        "ruff format --check .",
        "ruff check .",
        "mypy src tests",
        "pytest",
    ]
    for command in quality_commands:
        assert f"run: .venv/bin/python -m {command}\n" in text
    assert text.index("ai_session_handler.dev_bootstrap") < text.index(".venv/bin/python")
