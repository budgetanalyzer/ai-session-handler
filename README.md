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
or packaging changes. Keep the virtualenv container-local and use it for this
repository's quality gates.

## Entry Points

Repository-local entrypoints are exposed:

```bash
.venv/bin/python -m ai_session_handler --help
.venv/bin/ai-session-handler --help
```

Examples below use `ai-session-handler` for readability. If the virtualenv is
not active, use `/workspace/ai-session-handler/.venv/bin/ai-session-handler`.

## Command Model

The core command shape is:

```bash
ai-session-handler run \
  --plan docs/plans/plan-22.md \
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

Config is always read from `.ai-session-handler/config.json` in the inferred
workspace. Runner state is always stored as `.ai-session-handler/<plan-stem>.json`
in that same workspace.

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
capturing both in the transcript. Terminal marker blocks are captured for
parsing but hidden from the live console; the CLI prints the final phase result
once after state is updated. Runner-owned errors, including invalid inputs and
failed agent outcomes, are printed to stderr. Failed agent outcomes also print
the transcript path and recent transcript output for debugging. Transcript
headers include the agent working directory and rendered argv; if a process exits
without stdout or stderr, the transcript records that explicitly.

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

Run against another repository by passing the full plan path:

```bash
ai-session-handler run \
  --plan /workspace/my-project/docs/plans/plan-22.md
```

Print durable state and the latest transcript path:

```bash
ai-session-handler status --plan docs/plans/plan-22.md
```

If a phase stops, a later run refuses to continue by default and prints the
stored stop message, latest transcript path, and recent transcript output when
available. After human intervention, rerun that phase explicitly:

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

## Plan Format

Executable plans are Markdown files with explicit numbered phase headings:

```markdown
## Phase 1: Title
```

Any Markdown heading level is accepted, but the heading text must be
`Phase N: Title`. Phase numbers must be positive, unique, and strictly
increasing. Phase bodies are preserved exactly between phase headings.

Design documents are not executable plans. Headings such as `Stage`,
`Workstream`, and `Issue`, plus implementation-order lists, may describe useful
planning structure, but the runner only recognizes explicit phase headings.

See [docs/plan-format.md](docs/plan-format.md) for the canonical template and
active format contract.

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

For Codex high-reasoning runs, use the wrapper script from this repository's
container-local virtualenv:

```bash
ai-session-handler run \
  --plan /workspace/my-project/docs/plans/plan-22.md \
  --agent-cmd "/workspace/ai-session-handler/.venv/bin/ai-session-handler-codex-high --model gpt-5.5"
```

That wrapper is shipped by this project but remains outside runner internals. It
sets Codex's high-reasoning mode, runs `codex-lean exec` with non-colored output,
streams stdout/stderr as Codex runs while filtering live terminal marker blocks,
captures the final message, and re-emits the single terminal marker from the
final message. This keeps the core runner provider-agnostic while preserving the
runner's exactly-one-marker contract. Omit `--model` to use the Codex CLI
default or the `CODEX_MODEL` value already present in the environment.

## Exit Codes

- `0`: phase complete or all phases complete
- `2`: phase blocked
- `3`: phase needs clarification
- `4`: agent process failed, timeout, stop regex, missing marker, or multiple markers
- `5`: invalid plan, config, command template, or state

Invalid user inputs are printed to stderr with the file, command, marker, or
state key to fix when that context is available.

## Quality Gates

```bash
.venv/bin/python -m ruff format .
.venv/bin/python -m ruff check . --fix
.venv/bin/python -m mypy src tests
.venv/bin/python -m pytest
```
