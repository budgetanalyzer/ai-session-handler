# AI Session Handler Architecture Walkthrough

**Date:** 2026-07-07
**Participants:** Architect, Codex (GPT-5)
**Context:** The architect is an experienced Java architect with no Python background and asked for a boring, step-by-step walkthrough of this specific codebase: build flow, tests, user entry points, worker spawning and management, and overall runtime flow.

## The Insight

This repository is a small Python CLI package, not a service. Think "single-purpose command-line orchestrator" rather than application server.

The architecture is intentionally linear:

```text
parse CLI
  -> parse plan
  -> read state
  -> select phase
  -> render prompt
  -> spawn one subprocess
  -> capture output
  -> require one terminal marker
  -> update JSON state
  -> exit
```

There is no long-running daemon, worker pool, provider adapter layer, database, message broker, or framework runtime. Each selected plan phase gets one fresh external agent process.

## The Conversation

**Architect:** i am an experienced java architect with 24 years of experience. i have no experience using python. walk me thru this specific codebase and tell me the flow. step by step, how does the build work, how do tests work, what are the user entry points, how do the workers get spawned/managed, etc. i want a boring architecture walkthru.

**Codex:** Big picture first. This is a small Python CLI package, not a service. Think "single-purpose command-line orchestrator" rather than application server.

The package lives under `src/ai_session_handler`. The CLI accepts a markdown plan, finds the next incomplete phase, renders a worker prompt, spawns one external agent process, captures its output, writes durable state/transcripts, then exits.

### Build And Install Flow

Packaging is defined in `pyproject.toml`.

Key points:

- Runtime dependency list is empty.
- Dev dependencies are `mypy`, `pytest`, and `ruff`.
- It uses `setuptools`.
- It uses a `src/` layout.
- Console scripts are installed from `[project.scripts]`.

Setup is:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

In Java terms, `pyproject.toml` is roughly the combined `pom.xml` / Gradle build file. The editable install is like putting `src/main` on the classpath plus generating command wrappers under `.venv/bin`.

Relevant `pyproject.toml` details:

- Build backend: `setuptools.build_meta`
- Package name: `ai-session-handler`
- Python requirement: `>=3.12`
- Runtime dependencies: `[]`
- Dev dependencies: `mypy`, `pytest`, `ruff`
- Main console script: `ai-session-handler = "ai_session_handler.cli:main"`
- Codex wrapper console script: `ai-session-handler-codex-high = "ai_session_handler.provider_wrappers.codex_high_exec_filter:main"`
- Package discovery root: `src`
- Ruff target: Python 3.12, line length 100
- Mypy: strict mode
- Pytest: tests under `tests`, importlib import mode

### User Entry Points

There are two main ways in:

```bash
.venv/bin/ai-session-handler ...
.venv/bin/python -m ai_session_handler ...
```

The installed CLI points to:

```text
ai_session_handler.cli:main
```

The module entrypoint calls the same `main()`.

The CLI commands are defined in `src/ai_session_handler/cli.py`:

- `init`
- `run`
- `status`
- `--version`

`main()` parses args and dispatches to the right command handler:

- `run` -> `_run_command(args)`
- `status` -> `_status_command(args)`
- `init` -> `_init_command(args)`

### Generated Workspace Files

For a plan called `docs/plans/foo.md`, the runner stores generated files under the inferred workspace:

```text
.ai-session-handler/
  config.json
  foo.json
  prompts/
  transcripts/
```

Config path and state path are centralized in `src/ai_session_handler/config.py`:

- `default_config_path(workspace)`
- `default_state_path(workspace, plan_path)`

The plan determines the workspace. `cli.py` walks upward from the plan looking for one of:

```text
.ai-session-handler
.git
AGENTS.md
```

That means passing an absolute plan path in another repo causes this runner to use that repo's workspace, config, state, prompts, and transcripts.

### Run Command Flow

The main orchestration path starts in `_run_command()` in `cli.py`.

Step by step:

1. Infer workspace from `--plan`.
2. Resolve the plan path.
3. Compute default state/config paths.
4. Read `.ai-session-handler/config.json` if present.
5. Choose `--agent-cmd` from CLI or config.
6. Combine config `stop_on_regex` with CLI `--stop-on-regex`.
7. Build a `RunOptions` dataclass.
8. Call `run_phases()` in `runner.py`.
9. Print the runner outcome.
10. Return the outcome exit code.

Expected user errors are caught at the CLI boundary and converted into clear stderr messages plus documented exit codes. These include config errors, invalid command templates, plan parse errors, state errors, invalid regexes, and OS errors.

### Core Runner Flow

The central orchestration function is `run_phases(options: RunOptions)` in `src/ai_session_handler/runner.py`.

Inside `run_phases()`:

1. Validate `max_phases >= 1`.
2. Compile stop regexes up front so invalid regexes fail before state is written.
3. Parse markdown phases from the plan.
4. Read durable JSON state.
5. Verify the plan hash has not changed.
6. Select the next incomplete phase, or the stopped phase when `--retry-stopped` is provided.
7. If no phase remains, clear current phase/stop state, write state, and return `runner-complete`.
8. Mark the selected phase as current and write state.
9. Create a run id like `20260707T120102Z-phase-1`.
10. Compute transcript path under `.ai-session-handler/transcripts/`.
11. Build a `PromptContext`.
12. Write the worker prompt under `.ai-session-handler/prompts/`.
13. Render the same prompt text for stdin.
14. Call `run_agent_process(...)`.
15. Apply the process result to durable state.
16. Write final state.
17. Stop immediately if the outcome is not successful.
18. Otherwise continue only if `max_phases` allows another phase.

By default, `max_phases` is `1`, so one CLI invocation runs one phase and stops.

### Plan Parsing

Plan parsing lives in `src/ai_session_handler/phases.py`.

Plan phases are markdown headings matching:

```text
## Phase 1: Some Title
```

The regex is:

```python
r"^#+ Phase ([0-9]+): (.+)$"
```

That means the markdown heading level is flexible:

```text
# Phase 1: One
## Phase 2: Two
### Phase 3: Three
```

Parsed phases are immutable dataclasses with:

- `id`
- `number`
- `title`
- `body`
- `start_line`
- `end_line`

Phase ids are derived from the number:

```text
Phase 1 -> phase-1
Phase 2 -> phase-2
```

The parser preserves each phase body exactly between phase headings. It validates that:

- at least one phase exists
- phase numbers are positive
- phase numbers are not duplicated
- phase numbers are monotonically increasing

### State Model

State is modeled in `src/ai_session_handler/state.py`.

The main durable state types are frozen dataclasses:

- `RunnerState`
- `PlanRecord`
- `PhaseRef`
- `StopState`
- `LastRun`

This is the closest Python equivalent to simple immutable Java records. Changes are made by creating replaced copies rather than mutating fields in place.

`RunnerState` contains:

- `schema_version`
- `plan`
- `completed_phase_ids`
- `current_phase`
- `stop`
- `last_run`

Stop reasons are represented by a `StrEnum`:

- `blocked`
- `needs-clarification`
- `agent-failed`
- `missing-marker`
- `multiple-markers`
- `timeout`
- `stop-regex`

State is read from JSON by `read_state(path)`. If the state file does not exist, `read_state()` returns a new empty `RunnerState`.

State is written atomically by `write_state(path, state)`:

1. Create parent directory.
2. Serialize stable indented JSON.
3. Write to a temp file in the same directory.
4. Flush.
5. `fsync`.
6. Replace the target path.
7. Clean up temp file on failure.

Plan hash protection is handled by `ensure_plan_hash_matches(...)`.

Behavior:

- New state accepts the current plan hash.
- Existing state must match the current plan hash.
- If the plan changed, the runner fails closed.
- `--accept-plan-change` accepts the new hash only after verifying all previously completed phase ids still exist in the current plan.

Phase selection is handled by `select_next_phase(...)`.

Behavior:

- If state has a stop and `--retry-stopped` is not provided, raise `StoppedStateError`.
- If retrying a stopped phase, select that stopped phase.
- Otherwise select the first phase whose id is not in `completed_phase_ids`.
- If all phases are complete, return `None`.

### Worker Prompt

Prompt rendering lives in `src/ai_session_handler/prompts.py`.

The prompt includes:

- workspace path
- plan path
- state path
- run id
- transcript path
- selected phase id
- selected phase title
- execution instructions
- previous state summary
- selected phase body
- terminal marker grammar

The worker is instructed to:

- read repository instructions first, including the nearest `AGENTS.md`
- inspect current repository state before editing
- implement exactly the selected phase
- not proceed to later phases
- run validation commands listed in the selected phase
- avoid git workflow operations
- stop for clarification when an unplanned design decision is required
- emit exactly one terminal marker

The marker grammar is:

```text
<phase-complete>summary</phase-complete>
<phase-blocked>reason</phase-blocked>
<phase-needs-clarification>specific question for user</phase-needs-clarification>
```

The prompt is written to:

```text
.ai-session-handler/prompts/{run_id}.txt
```

The same prompt text is also piped to the worker process over stdin.

### Worker Spawning And Management

Workers are not persistent agents. There is no pool, daemon, scheduler, queue, socket protocol, or IPC system.

Each selected phase gets one fresh subprocess.

The spawn happens in `run_agent_process()` in `src/ai_session_handler/runner.py`.

Important details:

- The command template is rendered and split with `shlex.split`.
- Unsupported placeholders are rejected.
- The process is launched with `shell=False`.
- Working directory is the inferred workspace.
- `stdin`, `stdout`, and `stderr` are all pipes.
- Prompt text is written to stdin on a separate thread.
- stdout and stderr are read on separate threads.
- Reader threads send `_StreamItem` records into a `Queue`.
- The main loop drains the queue.
- Output is streamed live to the parent process stdout/stderr.
- Output is also written to the transcript.
- Output is accumulated into `combined_parts` for marker parsing.
- Timeout can terminate the child.
- Stop regexes can terminate the child.
- On timeout/stop-regex, the process is terminated, waited on, then killed if needed.

The command template supports these placeholders:

- `{prompt_file}`
- `{workspace}`
- `{run_id}`
- `{transcript_file}`
- `{state_file}`

The command template is not a shell script. It is split with `shlex.split`, then executed with `shell=False`.

Example:

```bash
.venv/bin/ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --agent-cmd "./scripts/run-agent --prompt {prompt_file} --run {run_id}"
```

This renders to an argv list and runs in the workspace directory.

### Terminal Marker Parsing

Terminal marker parsing lives in `src/ai_session_handler/markers.py`.

After the process exits successfully, the runner scans combined stdout/stderr for exactly one recognized marker.

Recognized markers:

```text
<phase-complete>...</phase-complete>
<phase-blocked>...</phase-blocked>
<phase-needs-clarification>...</phase-needs-clarification>
```

If there are no markers, the result is `missing-marker`.

If there is more than one marker, the result is `multiple-markers`.

The marker text is stripped and used as the run summary, blocked message, or clarification request.

### Applying Process Results

The process result is mapped to durable state by `apply_process_result(...)` in `runner.py`.

Result mapping:

- Controlled stop such as timeout or stop-regex -> stopped failure.
- Non-zero process exit -> `agent-failed`.
- Missing marker -> `missing-marker`.
- Multiple markers -> `multiple-markers`.
- Complete marker -> add phase id to `completed_phase_ids`, clear `current_phase`, clear `stop`.
- Blocked marker -> record `StopState(reason=blocked, ...)`.
- Needs-clarification marker -> record `StopState(reason=needs-clarification, ...)`.

Every result records `last_run` with:

- run id
- phase id
- status
- started timestamp
- finished timestamp
- exit code
- transcript path
- summary

### Exit Codes

Exit codes are defined in `runner.py`:

```text
0 = phase complete or all phases complete
2 = phase blocked
3 = phase needs clarification
4 = agent process failed, timeout, stop regex, missing marker, or multiple markers
5 = invalid plan, config, command template, or state
```

A blocked or clarification marker records `state.stop`.

Future `run` calls refuse to continue after a stopped state until `--retry-stopped` is passed.

### Status Command Flow

The `status` command lives in `_status_command()` in `cli.py`.

Step by step:

1. Infer workspace from `--plan`.
2. Resolve plan path.
3. Compute state path.
4. Parse phases.
5. Read state.
6. Verify plan hash.
7. If stopped, print stopped phase and reason.
8. If not stopped, print next phase or `all complete`.
9. Print latest transcript path or `none`.

Plan hash mismatch is treated as invalid state and returns exit code `5`.

### Init Command Flow

The `init` command writes example generated structure in the current directory:

```text
.ai-session-handler/config.json
.ai-session-handler/prompts/
.ai-session-handler/transcripts/
```

The example config contains:

```json
{
  "agent_cmd": "codex exec",
  "max_phases": 1,
  "timeout_seconds": 3600,
  "stop_on_regex": []
}
```

If config already exists, `init` fails rather than overwriting it.

### Transcripts

Transcript handling lives in `src/ai_session_handler/transcripts.py`.

Transcript file path:

```text
.ai-session-handler/transcripts/{run_id}.txt
```

Each transcript starts with metadata:

- run id
- phase id and title
- plan path
- state path
- workspace path
- started timestamp
- original agent command
- rendered argv

Then it contains captured stdout/stderr from the worker.

If a process exits without stdout or stderr, the transcript records that explicitly:

```text
[runner] process exited with code N without stdout/stderr output
```

CLI failure output prints recent transcript tail for debugging.

### Provider-Agnostic Core

The core runner does not know about Codex, Claude, OpenAI, Anthropic, or any provider-specific API.

It only knows:

```text
render command template
spawn argv
pipe prompt to stdin
stream stdout/stderr
parse terminal marker
record state
```

Provider-specific behavior belongs in wrapper scripts.

This repository includes one optional wrapper:

```text
ai-session-handler-codex-high
```

Its implementation is in:

```text
src/ai_session_handler/provider_wrappers/codex_high_exec_filter.py
```

That wrapper:

- reads the worker prompt from stdin
- sets `CODEX_REASONING_EFFORT=high`
- optionally accepts `--model` and passes it through as `CODEX_MODEL`
- runs `codex-lean exec`
- captures the final message
- sanitizes marker-like text from live stdout/stderr
- re-emits exactly one terminal marker from the final message when present
- returns the provider process exit code

The wrapper is intentionally outside runner internals. The runner remains provider-agnostic.

### Tests

Tests use `pytest`, configured in `pyproject.toml`.

The suite is mostly fake-agent based. It does not call real AI providers.

Test modules:

- `tests/test_runner.py`
  - process behavior
  - complete marker
  - blocked marker
  - needs-clarification marker
  - non-zero process exit
  - timeout
  - stop regex
  - missing marker
  - multiple markers
  - command template rendering
  - prompt file paths with spaces
  - large output
  - `max_phases`

- `tests/test_cli.py`
  - version output
  - module entrypoint
  - `init`
  - removed path options rejected
  - `status`
  - malformed state JSON
  - agent failure details
  - stopped-state transcript tail
  - subprocess-level acceptance
  - workspace inference from absolute plan path

- `tests/test_state.py`
  - missing state reads as empty state
  - state JSON round trip
  - plan hash computation
  - initial plan acceptance
  - next-phase selection
  - all-complete selection
  - plan hash mismatch rejection
  - retry stopped requirement
  - accept changed plan hash when completed phase ids still exist
  - reject changed plan hash when completed phase ids no longer exist
  - current phase reference update

- `tests/test_phases.py`
  - normal phase parsing
  - headings at any markdown level
  - empty phase body
  - duplicate phase numbers rejected
  - no phases rejected
  - non-matching headings ignored
  - non-monotonic phase numbers rejected

- `tests/test_prompts.py`
  - prompt rendering against a fixture
  - phase body without trailing newline
  - prompt file writing

- `tests/test_markers.py`
  - complete marker parsing
  - blocked marker parsing
  - needs-clarification marker parsing
  - missing marker rejected
  - multiple markers rejected

- `tests/test_codex_high_exec_filter.py`
  - wrapper filters live marker-like text
  - wrapper re-emits final terminal marker
  - module entrypoint fails usefully when `codex-lean` is unavailable

Quality gate commands:

```bash
.venv/bin/python -m ruff format .
.venv/bin/python -m ruff check . --fix
.venv/bin/python -m mypy src tests
.venv/bin/python -m pytest
```

The current observed test result during this walkthrough was:

```text
57 passed in 1.48s
```

## The Pattern

The whole system can be read as a small deterministic harness around nondeterministic worker sessions.

```text
Plan file = durable intent
State JSON = durable execution history
Prompt file = per-run worker contract
Transcript = audit trail
Agent command = provider-neutral worker boundary
Terminal marker = explicit phase result protocol
```

In Java architecture terms:

- `cli.py` is the adapter/controller layer.
- `runner.py` is the application service/orchestrator.
- `phases.py` is a parser/domain mapper for markdown plans.
- `state.py` is the durable state model plus repository-like JSON persistence.
- `prompts.py` is a deterministic template renderer.
- `markers.py` is a small protocol parser.
- `transcripts.py` is audit-log formatting.
- `provider_wrappers/` contains optional adapters outside the core domain.
- `tests/` are mostly black-box acceptance and unit tests with fake subprocess agents.

The design bias is conservative:

- no runtime dependencies
- no shell execution
- no provider-specific core logic
- no mutation of source plans as runner state
- one phase by default
- durable state before and after worker execution
- explicit terminal markers instead of inferred success
- stopped clarification is a first-class state, not failure
