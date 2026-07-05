# Provider-Agnostic Short-Context Task Runner

**Date:** 2026-07-05

## Summary

Build a new small repo for a local task runner whose only job is to execute one
fine-grained plan phase in a fresh agent session, record durable status, and
stop. It should stay provider-agnostic by invoking an arbitrary command template
rather than hardcoding Codex, Claude, or any other harness.

Default behavior: run exactly one phase, then exit for human review. Multi-phase
looping can exist behind an explicit `--max-phases N`, but the default remains
`1`.

## Language Decision

Use Python for v1, but treat that as a pragmatic implementation choice, not an
architectural commitment.

The runner's job is mostly local orchestration:

- parse markdown headings
- read and write JSON state
- render a narrow worker prompt
- spawn a subprocess
- stream/capture output
- parse terminal markers
- exit with meaningful status codes

That maps well to Python's standard library. A useful v1 runtime can be built with
`argparse`, `dataclasses`, `json`, `hashlib`, `pathlib`, `subprocess`,
`tempfile`, `threading`, `queue`, and `re`. No runtime dependency graph is
required, which keeps the tool auditable before trusting it in the AI control
path.

Java is a credible alternative, especially because the primary maintainer is
stronger in Java and the Budget Analyzer ecosystem is JVM-heavy. The cost is
mostly operational friction for this particular tool: more packaging ceremony,
Gradle/Maven wrapper files, distribution setup, and more boilerplate around a
small amount of file/process glue. That cost is worth paying if the runner grows
into a long-lived platform tool with richer domain rules, plugin APIs, or strong
type-driven extension points. It is probably not worth paying for the first
version.

Decision criteria:

| Criterion | Python v1 | Java v1 |
|-----------|-----------|---------|
| Small auditable repo | Strong | Moderate |
| Standard-library subprocess/file glue | Strong | Strong |
| Static type guarantees | Moderate with type hints | Strong |
| Packaging simplicity for local use | Strong | Moderate |
| Maintainer fluency | Moderate | Strong |
| Fits existing service ecosystem | Moderate | Strong |
| Fast iteration with agents | Strong | Moderate |

The compromise: keep the core contracts language-neutral. The plan parser, state
schema, prompt contract, transcript format, marker grammar, and exit codes should
be documented well enough that a Java port can replace the Python implementation
without changing user workflow.

## Python Quality Baseline

Use a modern Python quality stack even though the runtime stays standard-library
only.

Recommended v1 tools:

- `ruff`: linting, import sorting, pyupgrade-style modernization, and formatting
- `mypy`: strict static type checking
- `pytest`: test runner and fixtures
- `coverage.py`: optional once the test suite has enough shape to make coverage
  useful

Rationale:

- `ruff` is the closest Python equivalent to the ESLint + Prettier experience:
  fast, widely adopted, autofix-capable, and configured from `pyproject.toml`.
- `mypy --strict` gives this small tool the "boring typed core" feel that keeps
  Python from becoming loose scripting glue.
- `pytest` is the de facto Python test runner. `unittest` is viable, but pytest
  is easier for fake CLI/process fixtures and acceptance tests.
- Runtime dependencies and development dependencies are separate. The installed
  runner should not need Ruff, mypy, or pytest to run.

Suggested commands:

```bash
python -m ruff format .
python -m ruff check . --fix
python -m mypy src tests
python -m pytest
```

Suggested `pyproject.toml` baseline:

```toml
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "ai-session-handler"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[project.optional-dependencies]
dev = [
  "mypy",
  "pytest",
  "ruff",
]

[project.scripts]
ai-session-handler = "ai_session_handler.cli:main"

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]

[tool.mypy]
python_version = "3.12"
strict = true
packages = ["ai_session_handler"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

Reference resources:

- PEP 8 for Python style
- PEP 257 for docstrings
- Python Packaging User Guide for `pyproject.toml` and package structure
- Google Python Style Guide for a more prescriptive house-style reference
- Ruff documentation for lint rule selection and formatter behavior
- mypy documentation for strict typing patterns
- pytest documentation for fixtures and CLI integration tests

## Key Changes

- Create a new repo, default name: `ai-session-handler`.
- Implement a dependency-light Python 3 CLI using only the standard library.
- Use explicit, typed Python style: `dataclasses`, `Enum`, small modules, no
  framework magic, and strict type checking.
- Core command shape:

```bash
ai-session-handler run \
  --plan docs/plans/plan-22.md \
  --state .ai-session-handler/plan-22.json \
  --agent-cmd "your-agent-command-here" \
  --max-phases 1
```

- The runner writes a worker prompt to a temp file and supports
  provider-neutral invocation modes:
  - Default: pipe prompt to the agent process over stdin.
  - Optional placeholders in `--agent-cmd`: `{prompt_file}`, `{workspace}`,
    `{run_id}`, `{transcript_file}`, `{state_file}`.
- Do not implement provider adapters in v1. A user can wrap any provider CLI
  with a shell script if needed.
- Do not perform git operations. The runner may report dirty state if useful,
  but it must not commit, push, checkout, reset, clean, or stash.

## Plan And State Model

- The plan file remains the durable source of intent.
- Runner state lives separately under `.ai-session-handler/`, not by mutating the
  original plan.
- State records:
  - schema version
  - plan path and hash
  - current phase id/title, if a run is active or stopped
  - completed phase ids
  - stopped phase id, if any
  - stop reason: `blocked`, `needs-clarification`, `agent-failed`,
    `missing-marker`, `multiple-markers`, `timeout`, or `stop-regex`
  - clarification request, if any
  - last run id
  - timestamps
  - latest transcript path
  - validation summary reported by the worker
- v1 phase detection uses explicit markdown headings:

```markdown
## Phase 1: Name
## Phase 2: Name
```

- If no uncompleted phase is found, runner exits successfully with
  `runner-complete`.
- Phase ids are derived from the phase number, for example `phase-1`. The title
  is descriptive and may change, but duplicate phase numbers are invalid.
- A plan hash mismatch is a stop condition by default. The runner should fail
  with exit code `5` rather than continuing against a changed plan silently.
  Resuming after a plan edit should require an explicit user action such as
  `--accept-plan-change`, and the runner should verify that previously completed
  phase ids still exist.

Suggested state shape:

```json
{
  "schema_version": 1,
  "plan": {
    "path": "docs/plans/plan-22.md",
    "sha256": "abc123...",
    "accepted_at": "2026-07-05T00:00:00Z"
  },
  "completed_phase_ids": ["phase-1"],
  "current_phase": {
    "id": "phase-2",
    "title": "Implement state store"
  },
  "stop": null,
  "last_run": {
    "run_id": "20260705T120102Z-phase-2",
    "phase_id": "phase-2",
    "status": "phase-complete",
    "started_at": "2026-07-05T12:01:02Z",
    "finished_at": "2026-07-05T12:05:31Z",
    "exit_code": 0,
    "transcript_path": ".ai-session-handler/transcripts/20260705T120102Z-phase-2.txt",
    "summary": "Implemented state store and unit tests."
  }
}
```

## Plan Format Contract

v1 should require only phase headings. Everything else is advisory text passed to
the worker.

Minimum valid plan:

````markdown
## Phase 1: Parse Plans

Implement markdown phase parsing.

### Validation

```bash
python -m pytest
```
````

Recommended phase sections:

- `Goal`: what this phase changes
- `Scope`: files or behaviors likely touched
- `Non-goals`: explicit later-phase work
- `Required context`: docs or code the worker must read
- `Implementation notes`: constraints or known decisions
- `Validation`: commands the worker must run
- `Completion criteria`: observable state required before marker emission

The runner should include the entire selected phase body in the prompt. It should
not try to understand every section semantically in v1.

## Worker Prompt Contract

Each worker session receives a narrow prompt that says:

- Read the repo instructions first, including nearest `AGENTS.md`.
- Implement exactly the selected phase.
- Do not proceed to later phases.
- Inspect current repo state before editing.
- Follow the plan unless repo reality contradicts it.
- Run the validation commands listed in the phase.
- Do not run git commit/push/checkout/reset/clean/stash.
- Update only files required for the selected phase.
- End with exactly one terminal marker:

```xml
<phase-complete>summary</phase-complete>
<phase-blocked>reason</phase-blocked>
<phase-needs-clarification>specific question for user</phase-needs-clarification>
```

The runner must stop immediately on `phase-blocked` or
`phase-needs-clarification`.

Prompt inputs:

- workspace path
- plan path
- state path
- selected phase id/title
- selected phase body
- previous state summary
- transcript path for the current run
- exact terminal marker grammar

The prompt should be protocol-style, not explanatory prose. Use imperative
language and name the failure modes explicitly.

## Clarification And Compaction Stops

- User clarification is a first-class stop state, separate from failure.
- The worker prompt explicitly forbids design-changing guesses. If
  implementation requires an unplanned product, architecture, schema, API, or
  workflow decision, the worker must emit `<phase-needs-clarification>`.
- Provider-agnostic v1 cannot depend on a real pre-compaction hook because that
  is harness-specific and may not exist.
- Instead, v1 controls compaction risk by:
  - running one phase per fresh process
  - requiring small phases
  - stopping after each phase by default
  - supporting `--timeout`
  - supporting optional configurable output regexes such as
    `--stop-on-regex "compaction|context limit"` for harnesses that print
    warnings
- If a provider later exposes an actual pre-compact event, add it as an optional
  adapter/wrapper feature, not core runner logic.
- If a run stops on blocked/clarification, a later `run` should refuse to
  continue by default and print the stopped phase plus reason. Re-running that
  same phase should require an explicit flag such as `--retry-stopped`.

## Process Execution Details

- Treat `--agent-cmd` as a command template, not a shell script.
- Substitute only the supported placeholders:
  - `{prompt_file}`
  - `{workspace}`
  - `{run_id}`
  - `{transcript_file}`
  - `{state_file}`
- Split the substituted command with `shlex.split` and execute with
  `shell=False`.
- If a provider requires shell features, command substitution, environment
  bootstrapping, or provider-specific flags, put that logic in a wrapper script
  and pass the wrapper as `--agent-cmd`.
- Generate a prompt file for every run, even when the prompt is also piped over
  stdin.
- Default prompt delivery is stdin. `{prompt_file}` exists so wrappers or
  provider CLIs can choose file-based ingestion without changing runner logic.
- Stream stdout/stderr to the console while also writing a transcript. Do not
  wait until process exit to reveal output.
- Parse markers from the combined captured output. Exactly one recognized marker
  must appear. Zero or multiple markers are runner failures.
- On timeout, terminate the process, wait a short grace period, then kill it if
  needed. Record `timeout` in state and exit `4`.
- On `--stop-on-regex`, terminate the process when the regex matches streamed
  output. Record `stop-regex` in state and exit `4`.
- Store transcripts under `.ai-session-handler/transcripts/`.

Recommended transcript header:

```text
run_id: 20260705T120102Z-phase-2
phase: phase-2 Implement state store
plan: docs/plans/plan-22.md
state: .ai-session-handler/plan-22.json
started_at: 2026-07-05T12:01:02Z
agent_cmd: codex exec ...
---
```

## CLI Behavior

- `run`: select next incomplete phase, invoke one fresh agent process, parse
  marker, update state, exit.
- `status`: print current state in human-readable form.
- `init`: create `.ai-session-handler/config.json` with example values.
- `run --retry-stopped`: rerun the currently stopped phase after human
  intervention.
- `run --accept-plan-change`: update the stored plan hash after verifying that
  completed phase ids still exist.
- Exit codes:
  - `0`: phase complete or all phases complete
  - `2`: blocked
  - `3`: needs clarification
  - `4`: agent process failed or no terminal marker found
  - `5`: invalid plan/config/state

Config file should be optional. CLI flags override config values.

Suggested config:

```json
{
  "agent_cmd": "codex exec",
  "max_phases": 1,
  "timeout_seconds": 3600,
  "stop_on_regex": []
}
```

## Detailed Implementation Plan

Completion tracking note: when a phase is completed, mark its heading in this
document with `(Complete)` so the human-readable plan reflects implementation
progress. Runner state must still live under `.ai-session-handler/`; these
annotations are documentation only.

### Phase 1: Repository Skeleton (Complete)

- Create `ai-session-handler`.
- Add `README.md`, `AGENTS.md`, `pyproject.toml`, and a `src/ai_session_handler/`
  package.
- Expose both:
  - `python -m ai_session_handler`
  - console script `ai-session-handler`
- Add `tests/` using `pytest`.
- Add Ruff and mypy configuration to `pyproject.toml`.
- Document the one-phase default and provider-agnostic command-template model.

Suggested module layout:

```text
src/ai_session_handler/
  __init__.py
  __main__.py
  cli.py
  config.py
  markers.py
  phases.py
  prompts.py
  runner.py
  state.py
  transcripts.py
```

### Phase 2: Plan Parser (Complete)

- Implement `Phase` dataclass with `id`, `number`, `title`, `body`,
  `start_line`, and `end_line`.
- Parse headings matching `^## Phase ([0-9]+): (.+)$`.
- Preserve phase body exactly for prompt inclusion.
- Reject duplicate phase numbers.
- Return phases in file order but warn or fail if numbering is non-monotonic.
- Add tests for normal phases, empty body, duplicate phases, no phases, and
  headings that should not match.

### Phase 3: State Store (Complete)

- Implement `RunnerState` dataclasses.
- Read missing state as a new state.
- Write state atomically using temp file plus rename.
- Compute SHA-256 over the plan file bytes.
- Detect plan hash mismatch.
- Implement completed-phase selection: first parsed phase whose id is not in
  `completed_phase_ids`.
- Implement stopped-state refusal and `--retry-stopped`.
- Add tests for new state, completed phase selection, all-complete, hash
  mismatch, retry-stopped, and accept-plan-change.

### Phase 4: Prompt Builder (Complete)

- Render a protocol-style worker prompt from a template in code.
- Include selected phase body and previous state summary.
- Include exact marker grammar.
- Include explicit non-goals:
  - do not implement later phases
  - do not make design-changing guesses
  - do not perform git operations
- Write the prompt to a per-run prompt file under `.ai-session-handler/prompts/`.
- Add snapshot-style tests by comparing generated prompt text to fixture files.

### Phase 5: Process Runner And Transcripts

- Create run ids with UTC timestamp plus phase id.
- Substitute command placeholders.
- Execute with `subprocess.Popen(shell=False)`.
- Pipe prompt to stdin by default.
- Stream stdout/stderr with reader threads and tee output into the transcript.
- Enforce timeout and stop regex.
- Return process exit code and captured combined output to marker parser.
- Add fake-agent integration tests for stdout marker, stderr marker, nonzero
  exit, timeout, stop regex, and large output.

### Phase 6: Marker Handling And State Transitions

- Parse exactly one of:
  - `<phase-complete>...</phase-complete>`
  - `<phase-blocked>...</phase-blocked>`
  - `<phase-needs-clarification>...</phase-needs-clarification>`
- On complete:
  - append phase id to `completed_phase_ids`
  - clear `current_phase` and `stop`
  - record run summary
  - exit `0`
- On blocked:
  - set stop reason `blocked`
  - preserve current phase
  - record reason and transcript
  - exit `2`
- On needs clarification:
  - set stop reason `needs-clarification`
  - preserve current phase
  - record question and transcript
  - exit `3`
- On process failure, missing marker, multiple markers, timeout, or stop regex:
  - preserve current phase
  - record runner failure
  - exit `4`

### Phase 7: CLI Polish And Documentation

- Implement `status` output for:
  - all complete
  - next phase
  - stopped phase
  - plan hash mismatch
  - latest transcript
- Implement `init` to create `.ai-session-handler/config.json` with example values
  and required directories.
- Add README examples for Codex, Claude, and wrapper-script usage without
  hardcoding provider adapters.
- Review and update `AGENTS.md` so the operational instructions match the final
  implemented package name, commands, generated directories, quality gates, and
  repository workflow.
- Add a toy plan acceptance test fixture.
- Run the toy acceptance test with a fake agent before trying a real provider.

## Test Plan

- Quality gates:
  - `python -m ruff format .`
  - `python -m ruff check . --fix`
  - `python -m mypy src tests`
  - `python -m pytest`
- Unit test phase parsing from markdown headings.
- Unit test state read/write and plan hash mismatch detection.
- Unit test marker parsing for complete, blocked, clarification, missing marker,
  and multiple markers.
- Integration test with a fake agent command that:
  - emits `<phase-complete>`
  - emits `<phase-blocked>`
  - emits `<phase-needs-clarification>`
  - exits nonzero
  - produces no marker
- Acceptance test: run against a toy repo/plan and verify the runner executes
  only Phase 1, writes transcript/state, and stops.
- Acceptance test: run with `--max-phases 2` and verify exactly two fresh agent
  processes are invoked.
- Acceptance test: previous blocked state refuses to continue without
  `--retry-stopped`.
- Acceptance test: edited plan refuses to continue without
  `--accept-plan-change`.

## Out Of Scope For v1

- Provider-specific adapters.
- Agent API integrations.
- MCP integration.
- Parallel execution.
- Dependency graphs.
- Retries without human action.
- Automatic git worktrees or branches.
- Plan mutation/checklist editing.
- Semantic markdown understanding beyond phase headings.
- Running validation commands outside the worker process.
- Long-term task database behavior.

## Assumptions

- New repo is preferred over adding this to an existing repo.
- Default mode is one phase then stop.
- Provider integration is by command template, not built-in adapters.
- v1 does not solve generic task management, dependency graphs, parallelism,
  retries, or provider-specific session control.
- Fine-grained plans are still required; the runner enforces boundaries but
  cannot make an oversized phase safe.
