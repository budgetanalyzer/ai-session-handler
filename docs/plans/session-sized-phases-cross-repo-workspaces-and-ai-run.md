# Session-Sized Phases, Cross-Repository Workspaces, And `ai-run`

Make AI Session Handler plans allocate coherent portions of an agent context window, allow one plan
to run different phases in the repository whose `AGENTS.md` should shape that work, and provide the
opinionated `ai-run PLAN_NAME` command used in the Budget Analyzer sandbox.

## Outcomes

- Plan guidance defines a Phase as one session-sized context allocation containing one or more
  execution steps, with normal execution targeting roughly 50–60% of the available context window.
- Every phase declares exactly one execution workspace. The plan repository continues to own the
  config, state, prompts, and transcripts, while the worker process starts in the phase workspace.
- A fresh sandbox installs AI Session Handler globally from `/workspace/ai-session-handler` through
  an editable pipx installation.
- From any repository root, `ai-run PLAN_NAME` runs `./docs/plans/PLAN_NAME.md` quietly with the
  globally installed high-reasoning Codex wrapper.

## Product Decisions

1. Treat Phase as a session boundary, not a feature, issue, commit, or smallest independently
   describable task. A Phase may contain loosely related behavior when its execution steps reuse the
   same repository instructions, active code context, files, abstractions, and validation loop.
2. Target approximately 50–60% context use for an ordinary successful phase. This is planning
   guidance, not a runtime measurement or completion check; the remaining capacity is deliberate
   headroom for discovery, debugging, and validation.
3. Keep the worker restricted to its selected phase. Do not add live context-percentage detection,
   automatic continuation into later phases, sub-phase state, or a checkpoint marker in this work.
4. Require one `### Workspace` section in every phase. There is no compatibility mode for plans
   without it because existing plans have already been implemented.
5. Resolve a phase workspace relative to the plan repository root, not relative to the plan file.
   Require a relative path naming an existing directory with an `AGENTS.md`; `.` selects the plan
   repository and paths such as `../transaction-service` select a sibling repository.
6. Keep plan ownership separate from execution location. The plan repository still determines
   `.ai-session-handler/config.json`, durable state, generated prompts, and transcripts. Only the
   child process working directory and `{workspace}` command placeholder use the phase workspace.
7. Keep AI Session Handler provider-agnostic. The Codex-specific default belongs in the sandbox's
   `ai-run` launcher, not in the core CLI.
8. Define `ai-run PLAN_NAME [RUN_OPTIONS...]` as a repository-root command. `PLAN_NAME` is a bare
   filename stem without `.md` or path separators and resolves only to
   `$PWD/docs/plans/PLAN_NAME.md`. Remaining arguments pass to `ai-session-handler run`.
9. `ai-run` supplies `--quiet` and
   `--agent-cmd "ai-session-handler-codex-high"`. The existing `CODEX_MODEL` environment variable
   remains the optional model-selection mechanism; do not add another model configuration surface.
10. Install with `pipx install --force --editable /workspace/ai-session-handler` after the
    entrypoint has ensured the checkout exists. Ordinary Python source edits must be visible through
    the global command without a container rebuild or reinstall.

## Manual Execution Note

Run Phase 1 by itself. A runner process that parsed this plan before Phase 1 cannot acquire the new
phase-workspace behavior while it is still running. After Phase 1 completes, start a new invocation
for Phase 2 so the updated handler reparses `### Workspace` and starts that worker in
`/workspace/workspace`.

The sandbox source is read-only inside the running container. Phase 2 must use the workspace
repository's established staged-proposal workflow and requires the human to apply the reviewed
proposal on the host and rebuild the container before final fresh-container acceptance.

## Non-Goals

- Estimate or consume a provider's context window dynamically.
- Maximize context consumption or treat unused context as a failure.
- Execute more than one plan phase in the same worker process.
- Allow one phase to declare multiple workspaces or start workers at the generic `/workspace` root.
- Add a user-facing `--workspace` CLI option.
- Search for plans outside the current repository's `docs/plans/` directory.
- Add aliases for status, retry, plan creation, or arbitrary agent commands.
- Build a frozen Python executable, publish a package, or install development dependencies globally.
- Migrate old plans or add fallback behavior for missing `### Workspace` sections.

## Phase 1: Make Plans Session-Sized And Route Each Phase To Its Workspace

### Workspace

.

### Goal

Change the plan contract and runner so plan authors create session-sized phases with explicit
execution steps, and every worker starts in the single repository declared by its phase while all
runner-owned artifacts remain with the plan repository.

### Scope

- `docs/plan-format.md` and the canonical plan template it contains
- `AGENTS.md` and `README.md`
- `src/ai_session_handler/phases.py`
- `src/ai_session_handler/cli.py`
- `src/ai_session_handler/runner.py`
- `src/ai_session_handler/prompts.py`
- `src/ai_session_handler/transcripts.py`
- Focused fixtures and tests under `tests/`

### Required context

- Read `AGENTS.md`, `docs/plan-format.md`, and the plan parser, CLI, runner, prompt, transcript, and
  state modules before editing.
- Read the corresponding parser, CLI, runner, and prompt tests before changing types or fixtures.
- Preserve the current exactly-one-terminal-marker protocol, subprocess safety behavior, atomic
  state ownership, and plan hash behavior.
- Treat this plan as the first plan using the required workspace contract. Old completed plans do
  not need to remain parseable.

### Execution steps

1. Rewrite the plan-authoring guidance and canonical template around the explicit model
   `Plan -> Phase -> Execution steps`. Explain the 50–60% calibration target, deliberate headroom,
   the adjacent-phase merge check, and the conditions that justify a new phase: workspace/context
   change, independent decision or review gate, risky transition, or a combined working set that no
   longer fits comfortably.
2. Add a required `### Workspace` section to the canonical phase template. Update this repository's
   plan-authoring instructions and user documentation in the same change without duplicating a
   second competing format contract.
3. Extend the typed `Phase` model and Markdown parser to extract exactly one non-empty workspace
   value from every phase while preserving the complete phase body byte-for-byte for worker
   prompts. Report missing, duplicate, empty, absolute, or structurally invalid workspace
   declarations with the plan path and relevant line number.
4. Add a small typed workspace resolver at the boundary that owns filesystem validation. Resolve
   the declared relative path from the inferred plan repository, require an existing directory and
   an `AGENTS.md` at that exact root, and return a clear invalid-input error naming the phase and
   path when resolution fails.
5. Separate plan workspace from execution workspace in the orchestration types and call flow. Keep
   config/state/generated-file paths anchored to the plan workspace; start each subprocess with its
   resolved phase workspace as `cwd`; make `{workspace}` mean that execution workspace.
6. Make the distinction visible in worker prompts, transcript headers, status output, and error
   diagnostics using unambiguous names such as `plan_workspace_path` and
   `execution_workspace_path`. Ensure the worker's first repository instruction load comes from the
   execution workspace.
7. Update unit and subprocess acceptance tests, fake plans, prompt fixtures, and direct `Phase`
   constructions for the required workspace field. Cover two consecutive phases targeting
   different temporary repositories and prove that state, prompts, and transcripts stay under the
   plan repository while each fake worker observes the correct `cwd` and prompt paths.
8. Remove the old implicit same-workspace assumptions rather than retaining parallel fallback
   branches. Do not change the durable state schema unless implementation proves that restart
   correctness requires it; phase workspace remains durable intent in the hashed plan.

### Implementation notes

- Parse workspace metadata separately from the preserved phase body. Do not reconstruct or
  normalize the body passed to the worker.
- A required relative path keeps plans portable across workspace checkouts and makes `.` explicit.
- One plan may orchestrate several repositories, but each fresh worker still receives exactly one
  primary repository context.
- A stopped-phase retry must resolve the workspace again from the accepted plan before launching.
- Keep validation at the plan/filesystem boundary rather than scattering existence checks through
  subprocess code.

### Validation

Run the repository-local quality gates:

```bash
.venv/bin/python -m ruff format .
.venv/bin/python -m ruff check . --fix
.venv/bin/python -m mypy src tests
.venv/bin/python -m pytest
```

Also run focused CLI acceptance with a temporary plan repository and two sibling execution
repositories, each containing a distinct `AGENTS.md`, and verify:

- each worker's operating-system current directory is its declared workspace;
- prompt and transcript metadata distinguish plan and execution workspaces;
- config, state, prompts, and transcripts remain under the plan repository;
- missing or invalid workspace declarations fail before a worker is launched;
- a stopped phase retries in the same declared execution workspace.

### Completion criteria

- The canonical guidance unmistakably asks for session-sized phases containing execution steps and
  the 50–60% target.
- Every accepted phase has exactly one valid relative workspace declaration.
- Cross-repository phase routing works without `--workspace` and without moving runner-owned files
  out of the plan repository.
- Worker prompts and transcripts show the two workspace roles clearly.
- No compatibility path for workspace-less plans remains.
- Ruff formatting, Ruff lint, strict mypy, and the full pytest suite pass.

## Phase 2: Make The Editable Global Installation And `ai-run` Durable

### Workspace

../workspace

### Goal

Finish the sandbox-owned installation workflow so fresh containers expose the current handler code
globally and provide the short, opinionated `ai-run PLAN_NAME` command from any repository root.

### Scope

- Existing staged work under
  `tmp/ai-session-handler-global-cli-installation/proposed/ai-agent-sandbox/`
- A staged `ai-agent-sandbox/entrypoint.sh`
- A staged `ai-agent-sandbox/scripts/ai-run.sh`
- A staged `ai-agent-sandbox/Dockerfile`
- Workspace `README.md`, `docs/launch-options.md`, and `AGENTS.md` where operational guidance changes
- Focused shell/fake-command validation under `tmp/`
- Host application, container rebuild, and fresh-container acceptance

### Required context

- Read `/workspace/workspace/AGENTS.md`, `README.md`, `docs/launch-options.md`, the live sandbox
  `Dockerfile` and `entrypoint.sh`, and the relevant launcher scripts before editing.
- Inspect the existing staged global-installation proposal and its test artifacts. Reuse sound work,
  but do not assume the non-editable `pipx install --force` command in that proposal satisfies this
  plan.
- Respect the read-only `ai-agent-sandbox/` bind mount. Make sandbox-derived changes only in the
  established proposed tree under `tmp/` until the human applies them on the host.
- Do not call a real AI provider from automated tests.

### Execution steps

1. Reconcile the existing staged entrypoint with the current live entrypoint so unrelated newer
   sandbox behavior is preserved. After repository cloning, validate
   `/workspace/ai-session-handler/pyproject.toml` and run
   `pipx install --force --editable /workspace/ai-session-handler`. Fail with a specific message if
   the checkout, metadata, installation, or installed command is unusable.
2. Keep both package console scripts globally available: `ai-session-handler` and
   `ai-session-handler-codex-high`. Verify the editable environment imports
   `ai_session_handler` from `/workspace/ai-session-handler/src` rather than a copied site-packages
   snapshot.
3. Add a small Bash launcher installed as `/usr/local/bin/ai-run`. Support `ai-run --help`; otherwise
   require one bare plan name, reject path separators and unexpected `.md` suffixes with actionable
   messages, require `$PWD/docs/plans/PLAN_NAME.md`, and preserve spaces safely through quoting.
4. Have the launcher execute the global command with `run`, the resolved plan path, `--quiet`, and
   `--agent-cmd "ai-session-handler-codex-high"`, then append remaining run options unchanged. Do
   not `cd`, invoke a shell command template, hard-code a model, or add provider behavior to the
   Python runner.
5. Copy and mark the launcher executable in the staged Dockerfile using the sandbox's existing
   launcher conventions. Update the startup banner and human-facing launch documentation so the
   normal workflow is simply `cd /workspace/REPOSITORY` followed by `ai-run PLAN_NAME`.
6. Test the launcher with a fake `ai-session-handler` earlier on `PATH`. Assert exact argv and
   working directory behavior for a normal plan, forwarded `--max-phases 1`, `--retry-stopped`,
   missing arguments, invalid names, and missing plan files.
7. Test the entrypoint installation in isolated temporary pipx directories, including first
   install, repeated forced install, missing checkout/metadata, and installation failure. Run
   `shellcheck` on changed shell scripts and render the staged Compose/Docker configuration as far
   as the environment permits.
8. Present the staged diff for human review. After the human applies it to the host-owned sandbox,
   rebuild and reopen the devcontainer. In the fresh container, verify command discovery, editable
   source linkage, both console scripts, `ai-run --help`, and a fake-agent or otherwise provider-free
   end-to-end plan launch from a sibling repository.

### Implementation notes

- pipx is already installed in the sandbox image and exposes applications through
  `/home/vscode/.local/bin`; do not add another Python installer or modify the system Python.
- `--force` refreshes packaging metadata and console entrypoints on container startup;
  `--editable` makes ordinary source edits visible immediately afterward.
- The global package contains no development extras. The repository-local `.venv` remains the only
  development environment used for Ruff, mypy, and pytest.
- The launcher is intentionally provider-specific sandbox policy. Its use of the globally installed
  high-reasoning wrapper does not change the provider-agnostic command-template runner.
- If host application or container rebuild is still required, report that external action through
  the normal blocked/clarification flow and complete the phase only after fresh-container
  acceptance succeeds.

### Validation

Before host application:

```bash
shellcheck tmp/ai-session-handler-global-cli-installation/proposed/ai-agent-sandbox/entrypoint.sh
shellcheck tmp/ai-session-handler-global-cli-installation/proposed/ai-agent-sandbox/scripts/ai-run.sh
docker compose -f ai-agent-sandbox/docker-compose.yml config
```

Run the focused isolated pipx and fake-launcher checks created for this phase. After applying the
reviewed proposal and rebuilding the container, verify:

```bash
command -v ai-session-handler
command -v ai-session-handler-codex-high
command -v ai-run
ai-session-handler --version
ai-session-handler-codex-high --help
ai-run --help
pipx list
```

Use the pipx environment's Python to print `ai_session_handler.__file__` and confirm it resolves
under `/workspace/ai-session-handler/src/`. From a sibling repository root, use a fake agent command
or completed disposable plan to prove `ai-run PLAN_NAME` resolves only
`./docs/plans/PLAN_NAME.md`, preserves that repository as the plan workspace, and forwards optional
runner flags without invoking a real provider.

### Completion criteria

- Every fresh container installs or refreshes AI Session Handler globally from the checked-out
  source in editable mode.
- Both Python console scripts and `/usr/local/bin/ai-run` are available on `PATH` from arbitrary
  repository directories.
- `ai-run PLAN_NAME` has the exact opinionated behavior defined by this plan and produces clear
  boundary errors.
- Ordinary handler source edits are visible through the global command without reinstalling or
  rebuilding the container.
- The staged sandbox change has been reviewed, applied on the host, and validated in a rebuilt fresh
  container.
- Shell checks, isolated installer checks, fake-launcher tests, Compose rendering, and
  fresh-container acceptance all pass without contacting an AI provider.
