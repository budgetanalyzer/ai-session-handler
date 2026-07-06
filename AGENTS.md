# AI Session Handler

## Repository Purpose

This repository implements a container-local, provider-agnostic task runner for
short AI agent sessions. Its job is to run one fine-grained plan phase in a
fresh agent process inside the AI workspace container, record durable state and
transcripts, and stop for human review.

## Session Initialization

At the start of work:

1. Read this `AGENTS.md`.
2. Inspect the current repository structure before editing.
3. If code already exists, read the nearest relevant files before changing them.

Keep changes scoped to the requested phase, workflow, or user request.

## Core Product Constraints

- AI sessions run in the container, not on the user's workstation. Setup,
  installed entrypoints, agent wrapper scripts, generated state, and transcripts
  must be container-visible under `/workspace`.
- Prefer the simplest straightforward solution with convention over
  configuration. This project is not currently intended for broad distribution,
  so avoid distribution-oriented knobs, compatibility layers, or YAGNI
  flexibility unless the user explicitly asks for them.
- The plan file determines the workspace. Do not add or restore a user-facing
  `--workspace` selector; infer the workspace from `--plan` and keep generated
  handler files under that workspace's `.ai-session-handler/` directory.
- Keep the runner always provider-agnostic. Invoke arbitrary command templates;
  do not add Codex, Claude, OpenAI, Anthropic, or other provider adapters to
  core logic.
- Run one phase by default. Multi-phase execution must require an explicit
  option such as `--max-phases N`.
- Keep runtime dependencies empty for v1 unless the user explicitly accepts a
  dependency and the rationale is documented.
- Keep development dependencies separate from runtime dependencies.
- Do not mutate the source plan file as runner state. Store runner state under a
  dedicated generated directory.
- Do not perform git workflow operations from the runner. No commit, push,
  checkout, reset, clean, stash, branch creation, or automatic worktree behavior.
- Treat user clarification as a first-class stop state, not as failure.

## Implemented CLI Workflow

- Use `.venv/bin/ai-session-handler init` to create `.ai-session-handler/config.json`,
  `.ai-session-handler/prompts/`, and `.ai-session-handler/transcripts/`.
- Use `.venv/bin/ai-session-handler run --plan PATH --agent-cmd TEMPLATE` to run
  the next incomplete phase. `PATH` determines the workspace; pass a full plan
  path to run against another repository. The default `--max-phases` is `1`.
- Use `.venv/bin/ai-session-handler status --plan PATH` to inspect the next
  phase, stopped phase, plan hash mismatch, and latest transcript.
- Use `--retry-stopped` only after human intervention on a stopped phase.
- Use `--accept-plan-change` only after verifying a plan edit should become the
  new accepted plan identity.

## Python Baseline

Use modern, explicit Python. The implementation should feel like a small typed
system, not loose scripting glue.

- Require Python 3.12 unless the user changes the supported runtime.
- Use a `src/` layout for importable code.
- Put tests outside application code under `tests/`.
- Configure packaging and tools in `pyproject.toml`.
- Prefer small standard-library modules for orchestration: `argparse`, `dataclasses`,
  `json`, `hashlib`, `pathlib`, `subprocess`, `tempfile`, `threading`, `queue`,
  `re`, `shlex`, and related small stdlib utilities.
- Use `pathlib.Path` for filesystem paths.
- Read and write text with explicit `encoding="utf-8"`.
- Use timezone-aware UTC timestamps. Serialize them consistently.
- Use atomic writes for state files: write a temp file, flush it, then replace.
- Keep business logic out of the CLI entrypoint. The CLI parses arguments,
  calls typed functions, prints user-facing messages, and returns exit codes.
- Implement `main(argv: Sequence[str] | None = None) -> int`; `__main__.py`
  should call it through `SystemExit`.
- When creating Python project structure, tighten `.gitignore` for this
  repo. Keep generated Python artifacts out of version control: `.venv/`,
  `.mypy_cache/`, `.ruff_cache/`, `dist/`, `build/`, `*.egg-info/`,
  `__pycache__/`, `.coverage`, `htmlcov/`, and `.pytest_cache/`.

## Typing Standards

- Run mypy in strict mode.
- Annotate all public functions, methods, dataclasses, and module-level
  constants where the type is not obvious.
- Prefer precise stdlib types from `collections.abc` such as `Sequence`,
  `Mapping`, and `Iterable` for interfaces.
- Avoid `Any`. If it is unavoidable at a boundary such as decoded JSON, isolate
  it and validate into typed dataclasses immediately.
- Avoid broad `dict[str, object]` plumbing across modules. Convert raw data at
  IO boundaries.
- Use dataclasses for durable value objects such as phases, runner state, marker
  parse results, and process results.
- Use enums or literals for status values, stop reasons, and exit-code classes.
- Do not silence mypy with broad ignore comments. A narrow ignore must include
  the error code and a short reason.

## Formatting And Linting

Use Ruff as the single formatting, linting, pyupgrade, and import-sorting path.
Do not add Black, isort, Flake8, or autopep8 unless the user explicitly asks for
that tool split.

Expected commands in this workspace use the repository-local virtualenv. If
`.venv/` is missing, create/install the dev environment first rather than
falling back to global tooling. The virtualenv is container-owned for this
project; install the package and development tools in editable mode inside the
container:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

```bash
.venv/bin/python -m ruff format .
.venv/bin/python -m ruff check . --fix
.venv/bin/python -m mypy src tests
.venv/bin/python -m pytest
```

Recommended initial Ruff settings:

- `line-length = 100`
- `target-version = "py312"`
- `src = ["src", "tests"]`
- lint selections including `E`, `F`, `I`, `UP`, `B`, `SIM`, and `RUF`

Follow PEP 8 for style and PEP 257 for docstrings, but do not add docstrings to
obvious private helpers just to satisfy ceremony. Public modules and public API
functions should explain intent when the name and type signature are not enough.

## Testing Standards

Use pytest for tests.

- Prefer `tmp_path`, `monkeypatch`, `capsys`, and small fake commands over real
  providers or network calls.
- Test CLI behavior through both direct `main([...])` calls and subprocess-level
  acceptance tests where packaging or entrypoint behavior matters.
- Use fake agent commands for process-runner tests. Do not call real AI tools in
  automated tests.
- Keep tests deterministic. Avoid sleeps except where timeout behavior is the
  behavior under test, and keep those durations short.
- Cover success and stop paths: complete, blocked, needs clarification,
  non-zero process exit, timeout, stop-regex, missing marker, and multiple
  markers.
- For pytest configuration, prefer strict config and marker checking. For new
  projects, prefer importlib import mode to avoid `sys.path` surprises.

## Process Execution Safety

The runner executes user-supplied commands, so keep this surface narrow.

- Treat `--agent-cmd` as a command template, not a shell script.
- Substitute only documented placeholders.
- Split substituted commands with `shlex.split`.
- Execute with `shell=False`.
- Put provider-specific shell setup in wrapper scripts, not runner internals.
- Treat optional provider wrappers, such as a Codex high-reasoning wrapper, as
  container-visible scripts invoked through `--agent-cmd`. Do not add a core
  `codex-high` mode or other provider adapter to the runner.
- Provider wrappers should accept the worker prompt on stdin, preserve useful
  stdout/stderr for transcripts, return the provider process exit code, and
  preserve the exactly-one-terminal-marker contract expected by the runner.
- Stream stdout and stderr while also writing transcripts.
- Parse terminal markers from combined captured output after process completion
  or controlled stop.
- Require exactly one recognized terminal marker.
- On timeout, terminate the process, wait a short grace period, then kill it.
- Record failure and stop reasons durably before exiting.

## State, Files, And Schemas

- Keep the plan file as durable intent and state files as durable execution
  history.
- Include schema versions in persisted JSON.
- Preserve enough state for restart: accepted plan hash, completed phase ids,
  current phase, stop reason, last run id, transcript path, timestamps, and
  worker summary.
- Fail closed on plan hash mismatch unless an explicit accept-plan-change flow
  verifies completed phase ids still exist.
- Make JSON output stable and readable with indentation.
- Preserve phase body text exactly when building prompts.
- Report parse and validation errors with file paths and line numbers when
  possible.

## Error Handling

- Return documented exit codes instead of raising tracebacks for expected user
  errors.
- Use exceptions internally only when they simplify control flow or preserve
  context; convert them to clear CLI messages at the boundary.
- Keep error messages specific: name the invalid file, state key, phase id,
  marker, command, or placeholder.
- Do not guess through product, architecture, schema, API, or workflow
  decisions not present in durable documentation or the current user request.
  Stop and ask for clarification.

## Documentation Discipline

Update documentation in the same change as behavior, configuration, CLI, or
workflow changes.

- Update `README.md` when setup, commands, naming, usage, or examples change.
- Update `docs/` when architecture, state schema, prompt contract, marker
  grammar, exit codes, or operational workflows change.
- Update this `AGENTS.md` when agent instructions, quality gates, tooling, or
  repository workflow changes.
- Do not leave documentation updates as follow-up work.

## Python Research Baseline

The repository's Python baseline is consistent with current high-quality Python
practice:

- PyPA guidance favors `pyproject.toml` and explains why `src/` layout prevents
  accidental imports from the repository root.
- Ruff is commonly used as the fast formatter, linter, import sorter, and
  modernization tool.
- mypy strict mode is the right default for a small orchestration core where
  correctness matters more than dynamic convenience.
- pytest remains the practical default for CLI and filesystem-heavy tests; its
  guidance favors isolated test layouts, strict config where possible, and
  import modes that avoid path surprises.
- `uv` is commonly seen in modern Python workflows, but do not make it required
  for this repo unless the project intentionally adopts it. Maintain a standard
  `.venv/bin/python -m pip install -e ".[dev]"` workflow inside the container
  unless a package-manager decision is documented.

Reference sources for future checks: Python Packaging User Guide, PEP 8, PEP
257, Ruff documentation, mypy documentation, and pytest good integration
practices.
