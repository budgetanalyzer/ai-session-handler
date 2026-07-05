"""Configuration loading for AI Session Handler."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class HandlerConfig:
    """Optional configuration loaded from .ai-session-handler/config.json."""

    agent_cmd: str | None = None
    max_phases: int = 1
    timeout_seconds: float | None = 3600.0
    stop_on_regex: tuple[str, ...] = ()


class ConfigError(ValueError):
    """Raised when the optional config file is invalid."""


def default_config_path(workspace: Path) -> Path:
    """Return the default config path for a workspace."""
    return workspace / ".ai-session-handler" / "config.json"


def default_state_path(workspace: Path, plan_path: Path) -> Path:
    """Return the default state path for a plan."""
    return workspace / ".ai-session-handler" / f"{plan_path.stem}.json"


def read_config(path: Path) -> HandlerConfig:
    """Read config from JSON, returning defaults when the file is missing."""
    if not path.exists():
        return HandlerConfig()

    raw: object = json.loads(path.read_text(encoding="utf-8"))
    data = _expect_mapping(raw, source=str(path))

    return HandlerConfig(
        agent_cmd=_optional_string_at(data, "agent_cmd", source=str(path)),
        max_phases=_int_at(data, "max_phases", source=str(path), default=1),
        timeout_seconds=_optional_number_at(data, "timeout_seconds", source=str(path)),
        stop_on_regex=_string_tuple_at(data, "stop_on_regex", source=str(path)),
    )


def write_example_config(path: Path) -> None:
    """Create an example config file and required generated directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "prompts").mkdir(parents=True, exist_ok=True)
    (path.parent / "transcripts").mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ConfigError(f"{path}: config already exists")

    payload = {
        "agent_cmd": "codex exec",
        "max_phases": 1,
        "timeout_seconds": 3600,
        "stop_on_regex": [],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _expect_mapping(value: object, *, source: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{source}: expected config to be an object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ConfigError(f"{source}: expected config object keys to be strings")
        result[key] = item
    return result


def _optional_string_at(data: Mapping[str, object], key: str, *, source: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{source}: expected {key} to be a string or null")
    return value


def _int_at(data: Mapping[str, object], key: str, *, source: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{source}: expected {key} to be an integer")
    if value < 1:
        raise ConfigError(f"{source}: expected {key} to be at least 1")
    return value


def _optional_number_at(data: Mapping[str, object], key: str, *, source: str) -> float | None:
    value = data.get(key, 3600)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ConfigError(f"{source}: expected {key} to be a number or null")
    if value <= 0:
        raise ConfigError(f"{source}: expected {key} to be greater than 0")
    return float(value)


def _string_tuple_at(data: Mapping[str, object], key: str, *, source: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ConfigError(f"{source}: expected {key} to be a list")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigError(f"{source}: expected {key}[{index}] to be a string")
        result.append(item)
    return tuple(result)
