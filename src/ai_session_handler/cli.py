"""Command-line interface for AI Session Handler."""

from __future__ import annotations

import argparse
import sys
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Final, TextIO

from ai_session_handler import __version__
from ai_session_handler.config import (
    ConfigError,
    default_config_path,
    default_state_path,
    read_config,
    write_example_config,
)
from ai_session_handler.phases import PlanParseError, parse_phase_file
from ai_session_handler.plan_templates import PlanTemplateError, create_plan_template
from ai_session_handler.runner import (
    EXIT_AGENT_FAILED,
    EXIT_INVALID,
    CommandTemplateError,
    RunnerOutcome,
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

_TRANSCRIPT_TAIL_LINES: Final[int] = 40


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
    _add_plan_flag(run_parser)
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
    _add_plan_flag(status_parser)

    create_plan_parser = subparsers.add_parser("create-plan", help="create a plan scaffold")
    _add_plan_flag(create_plan_parser)

    subparsers.add_parser("init", help="create example config and directories")
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
    if args.command == "create-plan":
        return _create_plan_command(args)
    if args.command == "init":
        return _init_command(args)

    parser.print_help()
    return 0


def _add_plan_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", type=Path, required=True, help="markdown plan path")


def _run_command(args: argparse.Namespace) -> int:
    workspace = _infer_workspace_from_plan(args.plan)
    plan_path = _resolve_path(workspace, args.plan)
    state_path = default_state_path(workspace, plan_path)
    config_path = default_config_path(workspace)

    try:
        config = read_config(config_path)
        agent_cmd = args.agent_cmd or config.agent_cmd
        if agent_cmd is None:
            _print_error("--agent-cmd is required when config does not provide agent_cmd")
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
    except StoppedStateError as error:
        _print_stopped_state_error(error, state_path, workspace)
        return EXIT_INVALID
    except (
        CommandTemplateError,
        ConfigError,
        OSError,
        PlanParseError,
        StateError,
        ValueError,
    ) as error:
        _print_error(str(error))
        return EXIT_INVALID

    _print_run_outcome(outcome)
    return outcome.exit_code


def _status_command(args: argparse.Namespace) -> int:
    workspace = _infer_workspace_from_plan(args.plan)
    plan_path = _resolve_path(workspace, args.plan)
    state_path = default_state_path(workspace, plan_path)

    try:
        phases = parse_phase_file(plan_path)
        state = read_state(state_path)
        ensure_plan_hash_matches(state, plan_path, phases)
    except PlanHashMismatchError as error:
        print(f"plan hash mismatch: {error.plan_path}", file=sys.stderr)
        print(f"expected: {error.expected_sha256}", file=sys.stderr)
        print(f"actual:   {error.actual_sha256}", file=sys.stderr)
        return EXIT_INVALID
    except (OSError, PlanParseError, StateError) as error:
        _print_error(str(error))
        return EXIT_INVALID

    if state.stop is not None:
        print(f"stopped: {state.stop.phase_id} ({state.stop.reason.value})")
        if state.stop.clarification_request is not None:
            print(f"clarification: {state.stop.clarification_request}")
        if state.stop.message is not None:
            print(f"message: {state.stop.message}")
    else:
        next_phase = select_next_phase(state, phases)
        if next_phase is None:
            print("all complete")
        else:
            print(f"next phase: {next_phase.id} {next_phase.title}")

    if state.last_run is not None:
        print(f"latest transcript: {state.last_run.transcript_path}")
    else:
        print("latest transcript: none")
    return 0


def _create_plan_command(args: argparse.Namespace) -> int:
    workspace = _infer_workspace_from_plan(args.plan)
    plan_path = _resolve_path(workspace, args.plan).resolve()

    try:
        create_plan_template(plan_path)
    except PlanTemplateError as error:
        _print_error(str(error))
        return EXIT_INVALID

    print(plan_path)
    return 0


def _init_command(args: argparse.Namespace) -> int:
    workspace = Path.cwd().resolve()
    config_path = default_config_path(workspace)
    try:
        write_example_config(config_path)
    except (ConfigError, OSError) as error:
        _print_error(str(error))
        return EXIT_INVALID
    print(f"created {config_path}")
    return 0


def _print_error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _print_stopped_state_error(
    error: StoppedStateError,
    state_path: Path,
    workspace: Path,
) -> None:
    _print_error(str(error))
    if error.stop.clarification_request is not None:
        print(f"clarification: {error.stop.clarification_request}", file=sys.stderr)
    if error.stop.message is not None:
        print(f"message: {error.stop.message}", file=sys.stderr)
    print(f"agent cwd: {workspace}", file=sys.stderr)

    try:
        state = read_state(state_path)
    except (OSError, StateError) as detail_error:
        print(f"latest run details unavailable: {detail_error}", file=sys.stderr)
        return

    if state.last_run is None:
        return

    print(f"last run: {state.last_run.run_id} ({state.last_run.status})", file=sys.stderr)
    print(f"transcript: {state.last_run.transcript_path}", file=sys.stderr)
    _print_transcript_tail(Path(state.last_run.transcript_path), sys.stderr)


def _print_run_outcome(outcome: RunnerOutcome) -> None:
    if outcome.exit_code != EXIT_AGENT_FAILED:
        print(outcome.message)
        return

    print(outcome.message, file=sys.stderr)
    if outcome.state.last_run is not None:
        print(f"transcript: {outcome.state.last_run.transcript_path}", file=sys.stderr)
        _print_transcript_tail(Path(outcome.state.last_run.transcript_path), sys.stderr)


def _print_transcript_tail(path: Path, stream: TextIO) -> None:
    lines = _read_transcript_tail(path, max_lines=_TRANSCRIPT_TAIL_LINES)
    if not lines:
        return

    print(f"transcript tail (last {len(lines)} lines):", file=stream)
    for line in lines:
        stream.write(line)
        if not line.endswith("\n"):
            stream.write("\n")
    if _has_no_transcript_body(lines):
        print(
            "transcript has no captured stdout/stderr; check the agent command, cwd, "
            "and wrapper logging.",
            file=stream,
        )


def _read_transcript_tail(path: Path, *, max_lines: int) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as transcript:
            return list(deque(transcript, maxlen=max_lines))
    except OSError as error:
        return [f"unable to read transcript tail: {error}\n"]


def _has_no_transcript_body(lines: Sequence[str]) -> bool:
    try:
        header_end = lines.index("---\n")
    except ValueError:
        return False
    return all(line.strip() == "" for line in lines[header_end + 1 :])


def _infer_workspace_from_plan(plan_path: Path) -> Path:
    unresolved_plan = plan_path if plan_path.is_absolute() else Path.cwd() / plan_path
    resolved_plan = unresolved_plan.resolve()
    markers = (
        Path(".ai-session-handler"),
        Path(".git"),
        Path("AGENTS.md"),
    )
    for parent in (resolved_plan.parent, *resolved_plan.parents):
        if any((parent / marker).exists() for marker in markers):
            return parent

    if plan_path.is_absolute():
        return resolved_plan.parent
    return Path.cwd().resolve()


def _resolve_path(workspace: Path, path: Path) -> Path:
    return path if path.is_absolute() else workspace / path
