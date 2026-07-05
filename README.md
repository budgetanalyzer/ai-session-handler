# AI Session Handler

AI Session Handler is a local, provider-agnostic task runner for short AI agent
sessions. Its v1 goal is intentionally narrow: run one fine-grained plan phase
in a fresh agent process, record durable state and transcripts, then stop for
human review.

The runner does not include provider adapters. It invokes an arbitrary command
template supplied by the user, so Codex, Claude, local scripts, or any other
agent CLI can be used through the same core process model.

## Status

The provider-agnostic task-runner plan is implemented. The package includes
markdown phase parsing, durable state, worker prompt generation, subprocess
execution with transcripts, terminal marker handling, and `run`, `status`, and
`init` CLI commands.

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

## Command Model

The core command shape is:

```bash
ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --state .ai-session-handler/plan-22.json \
  --agent-cmd "your-agent-command-here" \
  --max-phases 1
```

By default, the runner executes exactly one phase and exits. Running multiple
phases requires an explicit option such as `--max-phases N`.

`--agent-cmd` is a command template, not a shell script. Supported placeholders
are:

- `{prompt_file}`
- `{workspace}`
- `{run_id}`
- `{transcript_file}`
- `{state_file}`

Provider-specific setup belongs in wrapper scripts, not in runner internals.

The worker prompt is always written under `.ai-session-handler/prompts/` and is
also piped to the agent process over stdin. Transcripts are written under
`.ai-session-handler/transcripts/`.

## Commands

Create the optional example config and generated directories:

```bash
ai-session-handler init
```

Run the next incomplete phase:

```bash
ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "your-agent-command"
```

Print durable state and the latest transcript path:

```bash
ai-session-handler status --plan docs/plans/plan-22.md
```

If a phase stops as blocked or needing clarification, a later run refuses to
continue by default. After human intervention, rerun that phase explicitly:

```bash
ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "your-agent-command" \
  --retry-stopped
```

If the plan file changes, the runner refuses to continue until the change is
accepted and completed phase ids are verified to still exist:

```bash
ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "your-agent-command" \
  --accept-plan-change
```

## Provider Examples

Codex can be invoked directly when its CLI reads work from stdin:

```bash
ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "codex exec"
```

Claude or another CLI can be used the same way if it accepts stdin:

```bash
ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "claude"
```

For provider-specific flags, shell setup, or file-based prompt ingestion, use a
wrapper script and keep that behavior outside the runner:

```bash
ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "./scripts/run-agent --prompt {prompt_file} --run {run_id}"
```

## Exit Codes

- `0`: phase complete or all phases complete
- `2`: phase blocked
- `3`: phase needs clarification
- `4`: agent process failed, timeout, stop regex, missing marker, or multiple markers
- `5`: invalid plan, config, command template, or state

## Quality Gates

```bash
python -m ruff format .
python -m ruff check . --fix
python -m mypy src tests
python -m pytest
```
