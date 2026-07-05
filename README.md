# AI Session Handler

AI Session Handler is a local, provider-agnostic task runner for short AI agent
sessions. Its v1 goal is intentionally narrow: run one fine-grained plan phase
in a fresh agent process, record durable state and transcripts, then stop for
human review.

The runner does not include provider adapters. It invokes an arbitrary command
template supplied by the user, so Codex, Claude, local scripts, or any other
agent CLI can be used through the same core process model.

## Status

This repository is currently at Phase 4: prompt builder. The package,
entrypoints, quality tooling, documentation baseline, markdown phase parsing,
durable state primitives, and worker prompt rendering are in place; process
runner behavior will be implemented in later phases from
`docs/plans/provider-agnostic-task-runner.md`.

## Install For Development

Requires Python 3.12 or newer.

```bash
python -m pip install -e ".[dev]"
```

## Entry Points

Both entrypoints are exposed:

```bash
python -m ai_session_handler --help
ai-session-handler --help
```

## Planned Command Model

The core command shape is:

```bash
ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --state .ai-session-handler/plan-22.json \
  --agent-cmd "your-agent-command-here" \
  --max-phases 1
```

By default, the runner will execute exactly one phase and exit. Running multiple
phases will require an explicit option such as `--max-phases N`.

`--agent-cmd` is a command template, not a shell script. The planned supported
placeholders are:

- `{prompt_file}`
- `{workspace}`
- `{run_id}`
- `{transcript_file}`
- `{state_file}`

Provider-specific setup belongs in wrapper scripts, not in runner internals.

## Quality Gates

```bash
python -m ruff format .
python -m ruff check . --fix
python -m mypy src tests
python -m pytest
```
