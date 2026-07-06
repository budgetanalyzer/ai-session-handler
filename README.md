# AI Session Handler

AI Session Handler is a container-local, provider-agnostic task runner for short
AI agent sessions. Its v1 goal is intentionally narrow: run one fine-grained
plan phase in a fresh agent process inside the AI workspace container, record
durable state and transcripts, then stop for human review.

The runner does not include provider adapters. It invokes an arbitrary command
template supplied by the user, so Codex, Claude, container-local scripts, or any
other agent CLI can be used through the same core process model.

All setup and execution commands for this project are intended to run in the
container that owns the workspace, not on the user's workstation.

## Status

The provider-agnostic task-runner plan is implemented. The package includes
markdown phase parsing, durable state, worker prompt generation, subprocess
execution with transcripts, terminal marker handling, and `run`, `status`, and
`init` CLI commands.

## Container Development Setup

Requires Python 3.12 or newer inside the container. From
`/workspace/ai-session-handler`:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

If `.venv/` already exists, rerun the editable install command after dependency
or packaging changes. Keep the virtualenv container-local and avoid global
Python tooling for this repo.

## Entry Points

Both entrypoints are exposed:

```bash
.venv/bin/python -m ai_session_handler --help
.venv/bin/ai-session-handler --help
```

## Command Model

The core command shape is:

```bash
.venv/bin/ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --state .ai-session-handler/plan-22.json \
  --agent-cmd "your-agent-command-here" \
  --max-phases 1
```

By default, the runner executes exactly one phase and exits. Running multiple
phases requires an explicit option such as `--max-phases N`.

`--agent-cmd` is a command template, not a shell script. The plan path determines
the workspace: `run` and `status` walk up from `--plan` to the nearest
`.ai-session-handler`, `.git`, or `AGENTS.md` marker. The rendered command runs
inside that workspace, so a full path to a plan in another repository uses that
repository's config, state, prompts, and transcripts by default. Use executables
and wrapper scripts that are visible from that container workspace, or pass
absolute container paths for shared tools. Supported placeholders are:

- `{prompt_file}`
- `{workspace}`
- `{run_id}`
- `{transcript_file}`
- `{state_file}`

Provider-specific setup belongs in wrapper scripts, not in runner internals.

The worker prompt is always written under `.ai-session-handler/prompts/` and is
also piped to the agent process over stdin. Transcripts are written under
`.ai-session-handler/transcripts/`.

The runner streams child stdout and stderr to the same streams while also
capturing both in the transcript. Runner-owned errors, including invalid inputs
and failed agent outcomes, are printed to stderr. Failed agent outcomes also
print the transcript path and recent transcript output for debugging. Transcript
headers include the agent working directory and rendered argv; if a process
exits without stdout or stderr, the transcript records that explicitly.

## Commands

Create the optional example config and generated directories:

```bash
.venv/bin/ai-session-handler init
```

Run the next incomplete phase:

```bash
.venv/bin/ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "your-agent-command"
```

Run against another repository by passing the full plan path:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/plan-22.md
```

Print durable state and the latest transcript path:

```bash
.venv/bin/ai-session-handler status --plan docs/plans/plan-22.md
```

If a phase stops, a later run refuses to continue by default and prints the
stored stop message, latest transcript path, and recent transcript output when
available. After human intervention, rerun that phase explicitly:

```bash
.venv/bin/ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "your-agent-command" \
  --retry-stopped
```

If the plan file changes, the runner refuses to continue until the change is
accepted and completed phase ids are verified to still exist:

```bash
.venv/bin/ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "your-agent-command" \
  --accept-plan-change
```

## Provider Examples

Codex can be invoked directly when its CLI reads work from stdin:

```bash
.venv/bin/ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "codex exec"
```

Claude or another CLI can be used the same way if it accepts stdin:

```bash
.venv/bin/ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "claude"
```

For provider-specific flags, shell setup, or file-based prompt ingestion, use a
wrapper script and keep that behavior outside the runner:

```bash
.venv/bin/ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "./scripts/run-agent --prompt {prompt_file} --run {run_id}"
```

For Codex high-reasoning runs, use the container-local wrapper script when it is
available in the target workspace:

```bash
.venv/bin/ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "/workspace/my-project/.ai-session-handler/codex-high-exec-filter.py"
```

That wrapper is intentionally outside runner internals. It sets Codex's
high-reasoning mode, runs Codex with non-colored exec output, captures the final
message, sanitizes marker-like text from live stdout/stderr, and re-emits the
single terminal marker from the final message. This keeps the core runner
provider-agnostic while preserving the runner's exactly-one-marker contract.

## Exit Codes

- `0`: phase complete or all phases complete
- `2`: phase blocked
- `3`: phase needs clarification
- `4`: agent process failed, timeout, stop regex, missing marker, or multiple markers
- `5`: invalid plan, config, command template, or state

## Quality Gates

```bash
.venv/bin/python -m ruff format .
.venv/bin/python -m ruff check . --fix
.venv/bin/python -m mypy src tests
.venv/bin/python -m pytest
```
