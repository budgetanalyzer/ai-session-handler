# Dependency Maintenance

**Status:** Repository preparation is complete: the uv pin, `.python-version`, `uv.lock`, frozen
bootstrap, pinned CI workflow, and `renovate.json` are checked in. Mend Renovate scope, GitHub
dependency settings, and hosted extraction evidence are not yet accepted, so dependency automation
is not active for this repository until the [human activation checklist](#human-activation-checklist)
succeeds.

The central [Budget Analyzer dependency-automation policy](https://github.com/budgetanalyzer/orchestration/blob/main/docs/dependency-automation.md)
owns ecosystem scope, scheduling, cost, Action-reference policy, administrator settings, and the
operator routine. This document owns this repository's dependency files, commands, Renovate
behavior, and failure triage.

## Owned dependency surfaces

- `pyproject.toml` is the direct compatibility contract. `project.dependencies` stays empty: the
  runner has no runtime packages. The `dev` extra bounds the Ruff, mypy, and pytest tool lines, and
  setuptools is bounded identically in `[build-system]` and the `build` dependency group so the
  editable install uses the locked backend without build isolation. `[tool.dependency-contract]`
  owns the single exact uv pin, annotated for Renovate.
- `.python-version` owns the exact Python patch. Keep it within the `requires-python` minor
  interval in `pyproject.toml`.
- `uv.lock` is the generated exact graph with standard PyPI sources and hashes. Never edit it by
  hand.
- `.github/workflows/build.yml` owns the GitHub Actions references, pinned to full commit SHAs
  with a release comment.
- `renovate.json` extends exactly the shared orchestration preset and owns only repository-specific
  extraction, lock maintenance, grouping, Python bounds, and review notes.

Rediscover current declarations instead of keeping a version inventory here:

```bash
rg -n '(^requires|requires-python|dependencies|renovate:|^uv =|uses:)' \
  pyproject.toml .github/workflows renovate.json
cat .python-version
```

## Single-source pins

The exact uv version appears only in `[tool.dependency-contract]` and the exact Python patch only
in `.python-version`. Documentation, workflows, and source refer to them by owner file or as
`.dependency-tools/uv-<pin>/`, never by literal. `tests/test_dependency_pin_references.py` enforces
this for `README.md`, `AGENTS.md`, active `docs/`, workflows, and `src/`. A uv or Python patch
update therefore changes only its owner file and `uv.lock`.

## Frozen normal setup

From the repository root, with any Python 3.12 interpreter:

```bash
PYTHONPATH=src python3 -m ai_session_handler.dev_bootstrap
```

The stdlib-only bootstrap (`src/ai_session_handler/dev_bootstrap.py`):

1. Reads and validates both pins.
2. Installs the pinned uv from standard PyPI into the ignored `.dependency-tools/uv-<pin>/`
   environment, then verifies `uv --version`.
3. Removes inherited package-index, constraint, uv Python-selection, and `VIRTUAL_ENV` overrides.
4. Runs `uv lock --check` and fails if `uv.lock` is missing or stale. It never refreshes the lock.
5. Runs `uv sync --frozen --extra dev --group build` for the exact `.python-version` runtime,
   making `.venv` match the lock, including the editable project.

Rerun it after pulling dependency changes. CI runs the same module after `actions/setup-python`
reads `.python-version`, then runs the quality gates through `.venv/bin/python`. The uv-created
`.venv` has no pip; do not install into it with pip.

## Intentional lock refresh

Refreshing `uv.lock` is an explicit maintenance operation. Tests, setup, and CI never do it.
Bootstrap first so the pinned uv exists, then resolve its path from the contract:

```bash
UV_BIN="$(.venv/bin/python -c 'from pathlib import Path; from ai_session_handler.dev_bootstrap import pinned_uv_executable, read_contract; print(pinned_uv_executable(Path.cwd(), read_contract(Path.cwd())))')"
```

Run refreshes in a shell without `UV_*`/`PIP_*` index or Python-selection overrides. For a full
refresh, for example after changing direct bounds or the Python patch:

```bash
"$UV_BIN" lock --upgrade --default-index https://pypi.org/simple --python "$(cat .python-version)"
```

For a narrow refresh of one package:

```bash
"$UV_BIN" lock --upgrade-package NAME --default-index https://pypi.org/simple \
  --python "$(cat .python-version)"
```

Add `--dry-run` to either command to preview changes without writing. After a uv pin change, rerun
the bootstrap first so `UV_BIN` names the new tool. Then review the manifest, versions, sources,
markers, and hashes in `uv.lock`, and run the complete validation below.

## Validation

```bash
PYTHONPATH=src python3 -m ai_session_handler.dev_bootstrap
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src tests
.venv/bin/python -m pytest
actionlint .github/workflows/*.yml
```

Validate `renovate.json` with the Renovate release and Node major pinned by orchestration's
`.github/workflows/dependency-automation-config.yml`. Set `RENOVATE_VERSION` to that workflow's
current pin, then run:

```bash
npx --yes --package="renovate@${RENOVATE_VERSION}" \
  renovate-config-validator --strict --no-global renovate.json
LOG_LEVEL=debug npx --yes --package="renovate@${RENOVATE_VERSION}" renovate \
  --platform=local --dry-run=extract --onboarding=false \
  --require-config=required --repository-cache=disabled
```

The local platform inspects only Git-tracked files in the current checkout; do not pass a
repository argument. Anonymous Action and Python-runtime lookups can fail or be rate-limited, so
treat a local run as extraction evidence only and leave lookup results to hosted acceptance.

## Renovate behavior

- The PEP 621 manager extracts `requires-python`, setuptools, and the dev tools, and maintains
  `uv.lock` as their lock file. The `pyenv` manager extracts `.python-version`. The GitHub Actions
  manager extracts each `uses:` reference and runner label. One narrow regex manager extracts only
  the annotated uv pin. No native manager is disabled, and the current files produce no overlap.
- Python updates are capped below the next minor line for both `pyenv` and `pep621`. Moving to a
  new Python minor is an intentional runtime migration of `requires-python`, `.python-version`,
  the Renovate cap, and the lock together.
- The two setuptools declarations are grouped as one build contract. The dev tools are not grouped
  because they do not share a release train.
- Weekly lock maintenance proposes admissible transitive refreshes in the shared Monday window.
- The shared preset keeps automerge off, requires dashboard approval for major updates, and limits
  pull-request concurrency.

Accept a proposal only when the manifest, `uv.lock`, `.python-version`, frozen bootstrap, and full
CI agree. A manifest-only change or a failed lock refresh is a failed update. For Action updates,
also review the release notes and run `actionlint`.

## Failure triage

- Bootstrap reports a stale lock: a manifest or Python pin changed without a refresh. Perform the
  intentional refresh above; do not relax the check or hand-edit `uv.lock`.
- Renovate cannot update the lock: inspect Python compatibility, the direct bounds, and the pinned
  uv's diagnostics. Do not merge the manifest alone.
- Extraction omissions or duplicates: compare manager, file, and package identities in the dry-run
  log. Narrow the regex to its one annotated declaration rather than disabling a native manager.
- Pin-reference test failure: replace the copied version literal with a pointer to its owner file.
- Do not add runtime packages, scanners, SBOM generation, or dependency-evidence workflows in
  response to a dependency update.

## Human activation checklist

Repository preparation performs no GitHub or Mend administrator action. Do not describe automation
as active until a human administrator has:

1. Published the reviewed changes to trusted `main` and recorded the exact commit and a successful
   `Build` run.
2. Added `budgetanalyzer/ai-session-handler` to the Mend Renovate Community App scope according to
   the central [GitHub and Mend settings](https://github.com/budgetanalyzer/orchestration/blob/main/docs/dependency-automation.md#github-and-mend-settings),
   with automerge disabled.
3. Enabled the GitHub dependency graph and Dependabot alerts while leaving Dependabot version
   updates and automatic security-update pull requests off.
4. Retained a sanitized hosted Renovate run that shows the shared preset resolving, PEP 621 with
   `uv.lock`, `pyenv`, GitHub Actions, and the uv regex extraction, plus weekly lock maintenance,
   with no fatal lookup errors.

Then update this status line and orchestration's activation record.
