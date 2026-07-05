"""Command-line interface for AI Session Handler."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from ai_session_handler import __version__
from ai_session_handler.config import (
    ConfigError,
    default_config_path,
    default_state_path,
    read_config,
    write_example_config,
)
from ai_session_handler.phases import PlanParseError, parse_phase_file
from ai_session_handler.runner import (
    EXIT_INVALID,
    CommandTemplateError,
    RunOptions,
    run_phases,
)
from ai_session_handler.state import (
    PlanHashMismatchError,
    StateError,
    StoppedStateError,
    ensure_plan_hash_matches,
    read_state,
    select_next_phase,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser."""
    parser = argparse.ArgumentParser(
        prog="ai-session-handler",
        description="Run one provider-agnostic AI agent plan phase and stop.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the package version and exit",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="run selected plan phase(s)")
    _add_plan_state_flags(run_parser)
    run_parser.add_argument("--agent-cmd", help="agent command template")
    run_parser.add_argument("--max-phases", type=int, help="maximum phases to run")
    run_parser.add_argument("--timeout", type=float, help="agent timeout in seconds")
    run_parser.add_argument(
        "--stop-on-regex",
        action="append",
        default=[],
        help="terminate the agent when streamed output matches this regex",
    )
    run_parser.add_argument(
        "--retry-stopped",
        action="store_true",
        help="rerun the currently stopped phase",
    )
    run_parser.add_argument(
        "--accept-plan-change",
        action="store_true",
        help="accept changed plan hash after validating completed phase ids",
    )

    status_parser = subparsers.add_parser("status", help="print current runner state")
    _add_plan_state_flags(status_parser)

    init_parser = subparsers.add_parser("init", help="create example config and directories")
    init_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="workspace root; defaults to the current directory",
    )
    init_parser.add_argument("--config", type=Path, help="config path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"ai-session-handler {__version__}")
        return 0

    if args.command == "run":
        return _run_command(args)
    if args.command == "status":
        return _status_command(args)
    if args.command == "init":
        return _init_command(args)

    parser.print_help()
    return 0


def _add_plan_state_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", type=Path, required=True, help="markdown plan path")
    parser.add_argument("--state", type=Path, help="runner state JSON path")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="workspace root; defaults to the current directory",
    )
    parser.add_argument("--config", type=Path, help="optional config JSON path")


def _run_command(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    plan_path = _resolve_path(workspace, args.plan)
    state_path = _state_path(workspace, args.state, plan_path)
    config_path = _config_path(workspace, args.config)

    try:
        config = read_config(config_path)
        agent_cmd = args.agent_cmd or config.agent_cmd
        if agent_cmd is None:
            print("error: --agent-cmd is required when config does not provide agent_cmd")
            return EXIT_INVALID

        stop_on_regex = tuple(config.stop_on_regex) + tuple(args.stop_on_regex)
        outcome = run_phases(
            RunOptions(
                workspace_path=workspace,
                plan_path=plan_path,
                state_path=state_path,
                agent_cmd=agent_cmd,
                max_phases=args.max_phases if args.max_phases is not None else config.max_phases,
                timeout_seconds=args.timeout
                if args.timeout is not None
                else config.timeout_seconds,
                stop_on_regex=stop_on_regex,
                retry_stopped=args.retry_stopped,
                accept_plan_change=args.accept_plan_change,
            )
        )
    except (
        CommandTemplateError,
        ConfigError,
        OSError,
        PlanParseError,
        StateError,
        ValueError,
    ) as error:
        print(f"error: {error}")
        return EXIT_INVALID

    print(outcome.message)
    return outcome.exit_code


def _status_command(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    plan_path = _resolve_path(workspace, args.plan)
    state_path = _state_path(workspace, args.state, plan_path)

    try:
        phases = parse_phase_file(plan_path)
        state = read_state(state_path)
        ensure_plan_hash_matches(state, plan_path, phases)
    except PlanHashMismatchError as error:
        print(f"plan hash mismatch: {error.plan_path}")
        print(f"expected: {error.expected_sha256}")
        print(f"actual:   {error.actual_sha256}")
        return EXIT_INVALID
    except (OSError, PlanParseError, StateError) as error:
        print(f"error: {error}")
        return EXIT_INVALID

    if state.stop is not None:
        print(f"stopped: {state.stop.phase_id} ({state.stop.reason.value})")
        if state.stop.clarification_request is not None:
            print(f"clarification: {state.stop.clarification_request}")
        if state.stop.message is not None:
            print(f"message: {state.stop.message}")
    else:
        try:
            next_phase = select_next_phase(state, phases)
        except StoppedStateError:
            next_phase = None
        if next_phase is None:
            print("all complete")
        else:
            print(f"next phase: {next_phase.id} {next_phase.title}")

    if state.last_run is not None:
        print(f"latest transcript: {state.last_run.transcript_path}")
    else:
        print("latest transcript: none")
    return 0


def _init_command(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    config_path = _config_path(workspace, args.config)
    try:
        write_example_config(config_path)
    except (ConfigError, OSError) as error:
        print(f"error: {error}")
        return EXIT_INVALID
    print(f"created {config_path}")
    return 0


def _resolve_path(workspace: Path, path: Path) -> Path:
    return path if path.is_absolute() else workspace / path


def _config_path(workspace: Path, path: Path | None) -> Path:
    return _resolve_path(workspace, path) if path is not None else default_config_path(workspace)


def _state_path(workspace: Path, path: Path | None, plan_path: Path) -> Path:
    return (
        _resolve_path(workspace, path)
        if path is not None
        else default_state_path(workspace, plan_path)
    )
