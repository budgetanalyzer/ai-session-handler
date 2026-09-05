# AI Session Handler

AI Session Handler is a container-local, provider-agnostic task runner for short
AI agent sessions. It runs each session-sized plan phase in a fresh agent process
inside the AI workspace container and records durable state and transcripts.

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

Active contracts are split by concern:

- [Plan format](docs/plan-format.md) defines executable plan structure and phase boundaries.
- [Session lifecycle and acceptance](docs/session-lifecycle.md) distinguishes process, provider,
  filesystem, persistence, and review guarantees.
- [State and recovery](docs/state-and-recovery.md) defines generated layout and interruption
  handling.
- [Worker result protocol](docs/worker-protocol.md) defines terminal results and durable handoffs.

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

The repository-local entrypoint is exposed at:

```bash
.venv/bin/ai-session-handler --help
```

Examples below use the direct virtualenv path and assume they are run from
`/workspace/ai-session-handler`.

## Command Model

The core command shape is:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/feature-rollout.md \
  --agent-cmd "your-agent-command-here"
```

By default, the runner executes every remaining phase, using a fresh agent
process for each one, until the plan completes or a phase stops. Set
`--max-phases 1` to stop after one successful phase, or use another positive
integer to cap the phases executed in one invocation.

A fresh child process is the runner guarantee. Whether the provider command starts a new
conversation, resumes one, compacts context, or uses native subagents is determined by that
command and its provider configuration. The handler also sets the child working directory but is
not a filesystem sandbox. See [Session lifecycle and acceptance](docs/session-lifecycle.md).

`--agent-cmd` is a command template, not a shell script. The plan path determines
the plan workspace: `run` and `status` first resolve `--plan` against the caller's
current directory, then walk up from that canonical path to the nearest
`.ai-session-handler`, `.git`, or `AGENTS.md` marker. Absolute paths and relative
paths from repository subdirectories or sibling repositories therefore use the
config, state, prompts, and transcripts belonging to the supplied plan.

Each phase declares its own execution workspace relative to the plan workspace.
The runner requires that directory to exist and contain an `AGENTS.md` at its
root, then starts that phase's fresh child process there. Use executables and
wrapper scripts visible from the execution workspace, or pass absolute container
paths for shared tools. Supported placeholders are:

- `{prompt_file}`
- `{workspace}` (the selected phase's execution workspace)
- `{run_id}`
- `{transcript_file}`
- `{state_file}`

Only those exact named fields are supported. Positional fields, attribute or
index access, conversions, and format specifications are rejected before state
is changed or a worker starts. The template is split into arguments before
placeholder values are substituted, so a path containing spaces or quotes stays
within its original argument. Use doubled braces for a literal brace, for
example:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/feature-rollout.md \
  --agent-cmd "./scripts/run-agent --metadata '{{\"mode\":\"fresh\"}}' --prompt={prompt_file}"
```

Malformed quoting, an empty command, or invalid placeholder syntax is an input
error with exit code 5.

Config is always read from `.ai-session-handler/config.json` in the inferred plan
workspace. Each canonical workspace-relative plan path has a SHA-256-derived key,
and its runner state is stored at
`.ai-session-handler/plans/<plan-key>/state.json`. Content hashes still detect
plan edits, but plan identity comes from the canonical path: two plans with the
same name or content do not share history. The config's `max_phases` value accepts
a positive integer or `null`; `null` is the default and runs the plan to
completion.

Provider-specific setup belongs in wrapper scripts, not in runner internals.

Each attempt gets a timestamped, UUID-backed id. Its prompt, transcript, and terminal outcome are
written under the owning plan directory in `prompts/<attempt-id>.txt`,
`transcripts/<attempt-id>.txt`, and `outcomes/<attempt-id>.json`; existing attempt artifacts are
never overwritten.
The prompt is also piped to the agent process over stdin. The state path included
in the worker prompt is read-only context: workers must not modify it and must
report their outcome through exactly one terminal result block. The block must begin at a line
boundary, contain a nonempty result, and end as the final non-whitespace content of either stdout
or stderr. Recognized tags in examples or diagnostics, malformed framing, extra blocks, or results
on both streams fail closed. See [Worker result protocol](docs/worker-protocol.md) for the complete
contract. The runner owns all durable state transitions derived from the result.

Every fresh worker receives the accepted plan's global preamble and exact selected phase body. It
also receives the latest relevant summary plus a compact, status-labeled index of earlier committed
outcome paths, so prior decisions and verification evidence remain discoverable without replaying
all transcripts. Terminal summaries are expected to name changed artifacts, validation commands
and results, decisions, limitations, and durable handoff references. Outcome bodies remain plain
text; the runner does not parse summary headings or generate compaction.

The runner writes and flushes each outcome JSON before atomically linking it from state. Only those
links make an outcome authoritative. A record left unreferenced by a crash or failed state write is
not later adopted as proof of success; the active attempt remains unresolved and requires the same
inspection and explicit retry flow.

Before launch, state records a prepared active attempt; after launch it records a running attempt
and a reuse-resistant Linux process identity when available. If the handler disappears before a
terminal result is durable, an ordinary run refuses to repeat that phase. `status` remains
read-only and reports the unresolved attempt and its artifacts. After inspecting partial changes
and stopping any surviving worker, use `--retry-stopped`; the runner refuses the retry while the
recorded worker can still be positively identified as alive. See
[State and recovery](docs/state-and-recovery.md) for lifecycle details.

Plans and generated state are release-scoped. Finish a partially executed plan with the same AI
Session Handler release that created its state; after upgrading, begin new work with fresh
generated state. The runner reads only its current generated formats and provides no migration,
compatibility reader, or supported mixed-release workflow. Existing generated files remain
user-owned evidence and may be archived manually, but the runner does not search for or interpret
files outside the current keyed layout.

The generated layout is:

```text
.ai-session-handler/
├── config.json
└── plans/
    └── <plan-key>/
        ├── state.json
        ├── prompts/
        │   └── <attempt-id>.txt
        ├── outcomes/
        │   └── <attempt-id>.json
        └── transcripts/
            └── <attempt-id>.txt
```

`run` uses nonblocking advisory locks on the canonical plan workspace and the selected execution
workspace. One plan workspace therefore has only one active handler invocation, and plans from
different roots cannot run phases concurrently when those phases target the same execution
directory. Contention returns exit code 5 before launching a worker or changing attempt state; it
does not queue or wait. No lock files are created in execution repositories. Locks coordinate only
cooperating handler invocations, and release after a crash does not establish that interrupted work
is complete or safe to retry. See [State and recovery](docs/state-and-recovery.md) for lock and
recovery details.

The runner reads child stdout and stderr in bounded chunks through a bounded queue, streams them to
the same live streams, and writes the complete output to the transcript. It incrementally retains
only terminal-protocol state and result text for marker validation, including each stream's
identity; ordinary diagnostic history is not duplicated in memory. Terminal marker blocks remain
in the transcript but are hidden from the live console, and interleaving between the independent
pipes cannot manufacture a result. A nonzero exit, timeout, or controlled stop overrides a
completion marker. The CLI prints the final phase result once after state is updated.
Runner-owned errors, including invalid inputs and failed agent outcomes, are printed to stderr.
Failed agent outcomes also print
the transcript path and recent transcript output for debugging. Transcript
headers distinguish the plan and execution workspace paths and include rendered
argv; if a process exits without stdout or stderr, the transcript records that
explicitly.

Each worker starts in a new POSIX session and process group. On a timeout, a
stop-regex match, an execution/streaming exception, or a catchable handler
interruption, the runner sends SIGTERM to the complete group, waits briefly,
then sends SIGKILL if any group members remain. It also cleans up ordinary
descendants that outlive a worker process so inherited pipes cannot hold the
runner open. SIGINT and SIGTERM are managed from immediately before launch
through group cleanup, pipe closure, and bounded thread joins. A signal during
startup or cleanup is deferred until the runner can finish owning and stopping
the group. The bundled Codex wrapper applies the same lifecycle scope to its
immediate child while keeping that child inside the handler-owned group.
See [State and recovery](docs/state-and-recovery.md) for the guarantee and its
limits.

Pass `--quiet` to suppress live child stdout and stderr while still capturing
the complete transcript, parsing terminal markers, and printing the final phase
result. This is useful when invoking the handler from another agent session,
where streamed child output would otherwise consume the parent session's
context.

Configured stop regexes intentionally keep their full-history meaning across combined stdout and
stderr. When at least one is enabled, the runner therefore retains all output for the attempt in
memory and searches the growing history after new chunks arrive and after the final drain. Large
outputs or expensive Python regular expressions can consume substantial memory and CPU; the
bounded queue keeps lifecycle checks responsive between drain batches but does not bound regex
execution itself. Without stop regexes, the durable transcript is the full diagnostic log.

## Commands

Run examples that omit `--agent-cmd` assume the inferred plan workspace's
`.ai-session-handler/config.json` supplies `agent_cmd`.

Create the optional example config and shared `plans/` directory:

```bash
.venv/bin/ai-session-handler init
```

Run all remaining phases:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/feature-rollout.md \
  --agent-cmd "your-agent-command"
```

Run only the next incomplete phase:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/feature-rollout.md \
  --agent-cmd "your-agent-command" \
  --max-phases 1
```

Run against another repository by passing the full plan path:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/feature-rollout.md
```

Relative paths are interpreted from the directory where the command is run.
For example, from `/workspace/my-project/tools`, address a plan in that
repository with:

```bash
/workspace/ai-session-handler/.venv/bin/ai-session-handler status \
  --plan ../docs/plans/feature-rollout.md
```

From `/workspace/my-project`, a sibling repository can be addressed with
`--plan ../other-project/docs/plans/feature-rollout.md`.

Run without echoing agent progress while retaining the durable transcript:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/feature-rollout.md \
  --quiet
```

Print durable state and the latest transcript and committed outcome paths:

```bash
.venv/bin/ai-session-handler status --plan /workspace/my-project/docs/plans/feature-rollout.md
```

`status` prints the exact keyed state path as well as the plan workspace and
selected execution workspace.

If a phase stops or has an unresolved active attempt, a later run refuses to continue by default
and prints the available recovery details. After inspecting the workspace and artifacts, answering
any clarification in durable plan or repository context, and stopping any surviving worker, rerun
that phase explicitly:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/feature-rollout.md \
  --agent-cmd "your-agent-command" \
  --retry-stopped
```

If the plan file changes, the runner refuses to continue until the change is
accepted and completed phase ids are verified to still exist:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/feature-rollout.md \
  --agent-cmd "your-agent-command" \
  --accept-plan-change
```

When clarification both stopped the phase and changed the plan, combine the two explicit actions:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/feature-rollout.md \
  --agent-cmd "your-agent-command" \
  --retry-stopped \
  --accept-plan-change
```

Do not place clarification in runner-owned state or outcome JSON. Put execution-wide intent in the
plan preamble and longer-lived design decisions in ordinary repository documentation.

Plan acceptance is snapshot-based. At invocation initialization, the runner hashes and parses one
read of the exact UTF-8 source bytes; CRLF line endings are preserved and included in the hash. It
uses that immutable snapshot for every phase selected by the invocation, then checks the source
again before each subsequent worker launch and before reporting full completion. A change found at
a checkpoint returns exit code 5 and launches no later worker. Any already recorded phase outcome
is retained because it describes work performed from the original snapshot. `--accept-plan-change`
only applies while initializing a new invocation, after completed phase ids are checked.

The runner also compares the stored canonical plan path independently of the
content hash. Renaming or moving a plan therefore requires an explicit decision
about whether to start fresh or carry current-release history forward. See
[State and recovery](docs/state-and-recovery.md) for the keyed layout and identity checks.

## Plan Format

Executable plans are Markdown files with explicit numbered phase headings:

```markdown
## Phase 1: Title
```

Any Markdown heading level is accepted, but the heading text must be
`Phase N: Title`. Phase numbers must be positive, unique, and strictly
increasing. Phase bodies are preserved exactly between phase headings.

Text before the first executable phase is retained as the plan's global preamble. Phase and
workspace headings inside fenced code blocks are examples, not executable structure. The supported
fences use at least three backticks or tildes and must be closed with the same character and at
least the opening length; an unclosed fence is an input error with its opening line reported.

A plan follows `Plan -> Phase -> Execution steps`. Each phase is one fresh worker process and
contains one or more concrete execution steps. Size phases first around coherent, independently
verifiable checkpoints, even when consecutive phases edit the same files. Context targets such as
25–35%, a roughly ten-minute duration, and a 40% warning point are only provisional calibration
aids: the runner does not measure them, and they are not universal quality thresholds.
Every phase also requires exactly one `### Workspace` section containing one
relative path such as `.` or `../transaction-service`; see the canonical guide
for phase-boundary and workspace rules. A phase performs execution work only in
that repository. Work requiring another repository must use a separate phase,
even when the combined work would otherwise fit in one session.

Design documents are not executable plans. Headings such as `Stage`,
`Workstream`, and `Issue`, plus implementation-order lists, may describe useful
planning structure, but the runner only recognizes explicit phase headings.

See [docs/plan-format.md](docs/plan-format.md) for the canonical template and
active format contract.

## Provider Examples

Provider-native continuation, compaction, and subagents can be used inside an appropriately
configured worker process. They complement durable fresh-phase checkpoints; the handler does not
schedule them or persist their internal thread structure. Keep model, tools, approvals, sandbox,
and conversation choices in the provider command or external wrapper rather than inferring them
from phase boundaries.

Codex can be invoked directly when its CLI reads work from stdin:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/feature-rollout.md \
  --agent-cmd "codex exec"
```

Claude or another CLI can be used the same way if it accepts stdin:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/feature-rollout.md \
  --agent-cmd "claude"
```

For provider-specific flags, shell setup, or file-based prompt ingestion, use a
wrapper script and keep that behavior outside the runner:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/feature-rollout.md \
  --agent-cmd "./scripts/run-agent --prompt {prompt_file} --run {run_id}"
```

For Codex high-reasoning runs, use the wrapper script from this repository's
container-local virtualenv:

```bash
.venv/bin/ai-session-handler run \
  --plan /workspace/my-project/docs/plans/feature-rollout.md \
  --agent-cmd "/workspace/ai-session-handler/.venv/bin/ai-session-handler-codex-high"
```

That wrapper is shipped by this project but remains outside runner internals. It sets Codex's
high-reasoning mode and requires a separately installed `codex-lean` executable. This repository
does not install, own, or modify `/usr/local/bin/codex-lean`. That external launcher chooses Codex
approvals, sandbox, tools, history, features, and defaults; inspect it before use. The Phase 11
inspection found danger-full-access execution and disabled web search and native multi-agent
features in this container. Those restrictions are independent of fresh phase execution.

The wrapper runs `codex-lean exec` with non-colored output,
streams stdout/stderr as Codex runs while filtering live terminal marker blocks and sanitizing
diagnostic marker text. It captures the final message and re-emits a terminal result only after
validating that file against the strict framing contract. This keeps the core runner
provider-agnostic while preserving the runner's exactly-one-result contract. Its child remains in
the process group created by the core runner, so lifecycle cleanup also reaches the provider
process. When run directly, the wrapper manages catchable termination signals from child launch
through its immediate-child cleanup. Pass `--model MODEL` only for an explicit override. Omitting
it preserves an existing `CODEX_MODEL` value or leaves selection to the external Codex
configuration.

A well-framed `phase-complete` result is still the worker's assertion, not automated review or
user approval. After `runner-complete`, inspect the changes and committed evidence, run final
validation independently, and decide whether to accept the implementation. The handler stores no
user-approval state; see the [manual final-review workflow](docs/session-lifecycle.md#manual-final-review).

## Exit Codes

- `0`: configured phase limit reached or all phases complete
- `2`: phase blocked
- `3`: phase needs clarification
- `4`: agent process, launch, execution IO, timeout, stop regex, or marker failure
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
