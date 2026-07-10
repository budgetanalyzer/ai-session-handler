"""Plan template rendering and creation."""

from __future__ import annotations

from pathlib import Path
from typing import Final

INCOMPLETE_PLAN_TEMPLATE_MARKER: Final[str] = "<!-- ai-session-handler-template: incomplete -->"


class PlanTemplateError(ValueError):
    """Raised when a plan template cannot be created safely."""


def render_plan_template(plan_path: Path) -> str:
    """Render the canonical incomplete Markdown plan template."""
    title = _title_from_plan_path(plan_path)
    return (
        f"{INCOMPLETE_PLAN_TEMPLATE_MARKER}\n"
        f"# {title}\n"
        "\n"
        "Remove the incomplete marker after replacing every placeholder.\n"
        "\n"
        "## Phase 1: TODO phase title\n"
        "\n"
        "### Goal\n"
        "\n"
        "TODO\n"
        "\n"
        "### Scope\n"
        "\n"
        "TODO\n"
        "\n"
        "### Non-goals\n"
        "\n"
        "TODO\n"
        "\n"
        "### Required context\n"
        "\n"
        "TODO\n"
        "\n"
        "### Implementation notes\n"
        "\n"
        "TODO\n"
        "\n"
        "### Validation\n"
        "\n"
        "TODO\n"
        "\n"
        "### Completion criteria\n"
        "\n"
        "TODO\n"
    )


def create_plan_template(plan_path: Path) -> None:
    """Create a new plan template without overwriting an existing file."""
    try:
        plan_path.parent.mkdir(parents=True, exist_ok=True)
    except FileExistsError as error:
        raise PlanTemplateError(f"{plan_path.parent}: parent path is not a directory") from error
    except OSError as error:
        raise PlanTemplateError(
            f"{plan_path}: could not create parent directories: {error}"
        ) from error

    try:
        with plan_path.open("x", encoding="utf-8") as plan_file:
            plan_file.write(render_plan_template(plan_path))
    except FileExistsError as error:
        raise PlanTemplateError(f"{plan_path}: plan already exists") from error
    except OSError as error:
        raise PlanTemplateError(f"{plan_path}: could not create plan template: {error}") from error


def _title_from_plan_path(plan_path: Path) -> str:
    words = plan_path.stem.replace("-", " ").replace("_", " ").strip()
    if not words:
        return "Plan"
    return words.title()
