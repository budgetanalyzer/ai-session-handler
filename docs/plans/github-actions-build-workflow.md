# GitHub Actions Build Workflow

Add a repository-native GitHub Actions build workflow that runs AI Session Handler's existing
Python quality gates on every push and pull request to `main`, with manual dispatch support. Match
the active workflow conventions in the neighboring orchestration, web, and transaction-service
repositories while keeping this repository's Python 3.12 and dependency policies authoritative.

## Reference conventions and decisions

- The compared repositories use `.github/workflows/build.yml`, the display name `Build`, an
  `ubuntu-latest` build job, `main` push and pull-request triggers, and `workflow_dispatch`.
- Use `actions/checkout@v6`, `actions/setup-python@v6`, and
  `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: 'true'`, consistent with the current action-generation
  convention used by the sibling workflows.
- Set workflow-level `permissions: contents: read`, matching the least-privilege pattern in the
  transaction-service workflow. The build needs no secrets or write permissions.
- Test only Python 3.12. It is the repository's required baseline, and the sibling build workflows
  test one declared project runtime rather than a version matrix.
- Install the package and development tools from the existing `.[dev]` extra. Do not introduce a
  requirements file, lockfile, package manager, runtime dependency, or CI-only dependency.
- Run formatting in check mode, then linting, strict type checking, and the full pytest suite as
  distinct named steps so a failed quality gate is immediately visible.
- Do not publish a release, upload an artifact, calculate coverage, or add a matrix, path filters,
  caching beyond setup-python's supported pip cache, concurrency policy, or third-party actions.
  Those behaviors are not part of the current repository contract or needed to validate this
  change.

## Phase 1: Add and document the Python build workflow

### Workspace

.

### Goal

Create a minimal, least-privilege GitHub Actions workflow that reproduces the repository's full
documented quality gates on the supported Python version.

### Scope

`.github/workflows/build.yml`, a concise CI description in `README.md`, and local verification of
the workflow's commands and repository diff.

### Non-goals

Release or package publishing, GitHub environments, secrets, coverage thresholds, artifact
uploads, multiple operating systems or Python versions, dependency-management changes, changes to
application behavior or tests, and edits to sibling repositories.

### Required context

Read `AGENTS.md`, `README.md`, and `pyproject.toml`. Treat the reference conventions and decisions
in this plan as the durable summary of the inspected sibling workflows; do not switch execution
workspaces to reread or edit those repositories. Confirm no existing workflow has appeared before
creating `.github/workflows/build.yml`, and preserve unrelated worktree changes.

### Execution steps

1. Create `.github/workflows/build.yml` with display name `Build`. Trigger it on pushes and pull
   requests targeting `main`, plus `workflow_dispatch`.
2. Grant only workflow-level `contents: read` permission and set
   `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: 'true'`. Define one `build` job on `ubuntu-latest`.
3. Check out the repository with `actions/checkout@v6`, then configure Python 3.12 with
   `actions/setup-python@v6` and its pip cache. Do not pass credentials or persist additional
   permissions.
4. Install the project and development dependencies with
   `python -m pip install -e ".[dev]"`. Keep dependency definitions in `pyproject.toml`.
5. Add separate, clearly named steps that run `python -m ruff format --check .`,
   `python -m ruff check .`, `python -m mypy src tests`, and `python -m pytest`. Keep the commands
   aligned with the local quality gates while ensuring CI never rewrites source files.
6. Add a short README section that identifies the workflow triggers, Python version, and enforced
   gates. Keep the existing container-local `.venv` development instructions unchanged.
7. Inspect the completed YAML and diff for valid indentation, exact action versions, read-only
   permissions, absence of secrets, and agreement among the workflow, README, `pyproject.toml`, and
   `AGENTS.md`.

### Implementation notes

Use the straightforward structure already established by the sibling `build.yml` files. Quote the
Python version and Node-action compatibility value to keep them strings. The setup-python pip
cache may use `pyproject.toml` as its dependency cache key; dependency installation must still run
on every job. Do not create a repository-local virtual environment in Actions because
`actions/setup-python` already exposes the isolated job interpreter; local contributor commands
remain `.venv/bin/python ...` as documented.

The formatting gate must use `--check`, not the local agent-oriented formatting command that
modifies files. No application test changes are expected solely to add CI. If a gate already fails
before the workflow change, report the concrete failure rather than weakening or omitting the gate.

### Validation

Run the exact CI operations locally through the existing repository virtual environment:

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src tests
.venv/bin/python -m pytest
git diff --check
```

If an installed GitHub Actions workflow validator is already available, run it against
`.github/workflows/build.yml`; do not add a dependency solely for that optional check. Finally,
compare each workflow `run` command with the successful local command and inspect the YAML event
and permission blocks directly.

### Completion criteria

`.github/workflows/build.yml` follows the documented sibling conventions, runs all four current
Python quality gates on Python 3.12 for `main` pushes and pull requests and manual dispatches, and
has read-only repository permissions with no secrets. The README accurately describes CI, all
required local validation passes or any pre-existing failure is reported with evidence, and the
change contains no unrelated behavior, dependency, release, or sibling-repository edits.
