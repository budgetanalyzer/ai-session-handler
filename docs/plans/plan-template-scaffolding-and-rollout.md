# Plan Template Scaffolding And Workspace Rollout

## Goal

Add a safe `ai-session-handler create-plan --plan PATH` workflow that creates a canonical,
phase-marked Markdown execution-plan template, rejects incomplete templates, improves diagnostics
for unmarked planning documents, and becomes the single format source of truth referenced by
repository `AGENTS.md` files across `/workspace`.

## Dependency

This plan depends on all phases of
`/workspace/workspace/docs/plans/ai-session-handler-global-cli-installation.md`. Before starting
Phase 1, verify from this repository and at least one sibling repository that:

```bash
command -v ai-session-handler
ai-session-handler --version
```

resolve successfully through the sandbox-managed global installation. If the workspace plan has
not been implemented and accepted in a fresh container, stop with `phase-needs-clarification`
rather than distributing instructions that reference an unavailable command.

The global installation will initially expose the handler version that predates `create-plan`.
After implementing the feature, this plan explicitly refreshes the pipx installation and verifies
the new subcommand before changing sibling repositories.

## Product Decisions

1. Use a dedicated `create-plan` subcommand.

   The command shape is:

   ```bash
   ai-session-handler create-plan --plan docs/plans/example.md
   ```

   Do not overload `init`, which already initializes handler configuration and generated runtime
   directories.

2. Keep explicit numbered Phase headings as the execution contract.

   Generated execution units use `## Phase N: Title`. Do not infer phases from headings such as
   Stage, Workstream, Issue, or numbered lists. Those headings may be reported as diagnostic
   candidates, but they must never be executed without explicit Phase markers.

3. Generate a safe incomplete scaffold.

   The template contains a versioned or otherwise exact handler-owned incomplete-template marker,
   one example Phase, and obvious placeholders. `run` and `status` must reject the file while the
   marker remains. The author removes the marker only after replacing every placeholder and adding
   all intended phases.

4. Never overwrite an existing plan.

   `create-plan` creates missing parent directories but fails with the standard invalid-input exit
   code when the target already exists. Do not add `--force` in this change.

5. Derive the document title without adding required authoring flags.

   Use the plan filename stem to produce a readable initial H1. Authors may edit it afterward. Keep
   `--plan` as the only required option.

6. Keep plan generation separate from runner state.

   Creating a plan must not initialize configuration, create `.ai-session-handler/`, accept a plan
   hash, or create state and transcript files.

7. Distinguish execution plans from other planning documents.

   Repository instructions apply only to implementation or execution plans intended for AI Session
   Handler. They must not force personal, architectural, discovery, or long-range strategy documents
   into executable phases unless that is their intended use.

## Template Contract

The generated Markdown should contain concise author guidance and this structure:

```text
<!-- ai-session-handler-template: incomplete -->
# Example

Remove the incomplete marker after replacing every placeholder.

 ## Phase 1: TODO phase title

### Goal

TODO

### Scope

TODO

### Non-goals

TODO

### Required context

TODO

### Implementation notes

TODO

### Validation

TODO

### Completion criteria

TODO
```

The single leading space before the illustrative Phase heading prevents this implementation plan's
current parser from treating the example as one of its own phases; the generated file must not emit
that space. The implementation may adjust wording to keep the generated Markdown concise, but it
must retain the incomplete marker, canonical Phase heading, and recommended sections. The template
is package-owned code or package data; it must not be copied into each consuming repository.

## Non-Goals

- Rewrite or automatically normalize existing free-form planning documents.
- Guess execution boundaries from Stage, Workstream, Issue, or list-item wording.
- Mutate an existing plan to add markers.
- Add provider-specific plan generation or invoke an AI provider from `create-plan`.
- Add runtime dependencies.
- Add a user-facing workspace selector.
- Change durable state schema or phase identifiers.

## Phase 1: Implement Safe Plan Template Creation

### Goal

Implement typed business logic for rendering and exclusively creating a canonical incomplete plan
template, including fail-closed recognition of that template by the phase parser.

### Scope

- A focused module under `src/ai_session_handler/` for the template marker, rendering, and file
  creation
- `src/ai_session_handler/phases.py`
- Focused tests under `tests/`

### Required context

- Read `AGENTS.md`, `src/ai_session_handler/phases.py`, `src/ai_session_handler/config.py`, and the
  state atomic-write conventions before choosing module boundaries.
- Read `tests/test_phases.py` and existing filesystem tests for project style.

### Implementation

1. Define one exact incomplete-template marker as a typed module constant shared by generation and
   parsing. Avoid duplicating the sentinel text across modules.
2. Add a small typed function that renders the UTF-8 Markdown template deterministically from a plan
   path. Convert hyphens and underscores in the filename stem into a readable H1 without trying to
   infer domain semantics.
3. Add a typed creation function that creates parent directories and writes the new file without
   overwriting an existing path. Convert expected filesystem conflicts into a specific domain error
   that the CLI can report without a traceback.
4. Preserve the current no-runtime-dependency baseline and keep this business logic out of the CLI
   entrypoint.
5. Make `parse_phases` reject the exact incomplete-template marker before returning executable
   phases. The error must tell the author to replace placeholders and remove the marker.
6. Do not reject arbitrary uses of the word `TODO` after the marker is removed; the explicit marker
   is the fail-closed boundary.

### Tests

- Rendering is deterministic and contains the exact marker, derived H1, canonical Phase heading,
  and every recommended section.
- Creating a nested target creates only the requested parent directories and plan file.
- Creating a target that already exists fails and preserves its bytes exactly.
- No `.ai-session-handler` directory or runner state is created.
- A generated template is rejected by `parse_phase_file` with an actionable error.
- Removing the marker produces a plan whose Phase 1 body is parsed normally and preserved exactly.

### Validation

```bash
.venv/bin/python -m pytest tests/test_phases.py tests/test_plan_templates.py
.venv/bin/python -m mypy src tests
```

### Completion criteria

- Template behavior is deterministic, typed, and independently testable.
- Existing files cannot be overwritten through the template API.
- Incomplete generated plans cannot be selected by `run` or `status`.
- Existing valid phase parsing remains unchanged.

## Phase 2: Add The Create-Plan CLI Workflow

### Goal

Expose template creation through the stable global command shape without changing `init`, `run`, or
`status` semantics.

### Scope

- `src/ai_session_handler/cli.py`
- `tests/test_cli.py`
- Subprocess-level entrypoint tests where packaging behavior matters

### Implementation

1. Add the top-level `create-plan` argparse subcommand with required `--plan PATH`.
2. Resolve a relative target against the inferred repository workspace using the existing plan-path
   rules. An absolute path must continue to infer its owning workspace from `.ai-session-handler`,
   `.git`, or `AGENTS.md` markers.
3. Call the typed template creation function, print the created absolute path on success, and return
   exit code `0`.
4. Convert an existing target, invalid target path, and expected filesystem errors into clear stderr
   messages and exit code `5`, consistent with other invalid inputs.
5. Do not read handler configuration, require an agent command, initialize generated directories, or
   modify runner state.
6. Keep `main(argv: Sequence[str] | None = None) -> int` and existing entrypoints unchanged.

### Tests

- Direct `main([...])` coverage for relative and absolute target paths.
- The output path and generated bytes are deterministic.
- Existing-file rejection returns exit code `5`, writes only to stderr, and preserves the file.
- The command works before `ai-session-handler init` and creates no generated handler directory.
- Removed or unrelated options remain rejected by argparse.
- A subprocess acceptance test executes `python -m ai_session_handler create-plan`, then confirms
  `status` rejects the incomplete marker rather than selecting Phase 1.
- Existing `init`, `run`, `status`, and `--version` tests remain green.

### Validation

```bash
.venv/bin/python -m pytest tests/test_cli.py tests/test_plan_templates.py tests/test_phases.py
.venv/bin/python -m mypy src tests
```

### Completion criteria

- The documented command creates a safe plan scaffold from any workspace repository.
- Existing plans are never overwritten.
- Creation has no runner-state or configuration side effects.
- Existing CLI workflows retain their behavior.

## Phase 3: Improve Diagnostics, Documentation, And Global Acceptance

### Goal

Make format failures self-correcting, document the new authoring workflow, pass all quality gates,
and refresh the sandbox-managed global installation before repository instructions reference it.

### Scope

- `src/ai_session_handler/phases.py` and focused tests
- `README.md`
- `AGENTS.md`
- Active format-contract documentation under `docs/`
- The current container's sandbox-managed pipx installation for acceptance testing only

### Implementation

1. Improve the no-phase error to show the canonical example `## Phase 1: Title` and direct the user
   to `ai-session-handler create-plan --plan PATH`.
2. Detect likely Stage, Workstream, or Issue Markdown headings only for diagnostics. Report their
   source line numbers as possible planning headings while explicitly refusing to interpret them as
   executable phases. Bound the number of candidates in one error so a large document remains
   readable.
3. Add tests based on a document containing global Stage headings, repeated issue-local stages,
   Issue headings, and an implementation-order list. Confirm the parser still rejects it rather than
   choosing an arbitrary decomposition.
4. Update README setup, command reference, examples, exit behavior, and plan-format documentation.
   Explain the incomplete marker, no-overwrite rule, and distinction between design documents and
   executable plans.
5. Update this repository's `AGENTS.md` implemented workflow and plan-authoring guidance to use the
   globally available command while retaining repository-local `.venv` quality-gate commands.
6. Update active plan-format contract documentation in the same change. Do not rewrite historical
   conversation records merely to change examples.
7. Reinstall the completed package through the sandbox's supported global pipx setup, then verify
   the installed command from this repository and a sibling repository. Do this only after all code
   and focused tests pass.

### Validation

Run the complete repository quality sequence:

```bash
.venv/bin/python -m ruff format .
.venv/bin/python -m ruff check . --fix
.venv/bin/python -m mypy src tests
.venv/bin/python -m pytest
```

Refresh and accept the global installation through the mechanism established by the dependency
plan. Then, from a sibling repository, use a disposable path outside tracked documentation:

```bash
ai-session-handler --version
ai-session-handler create-plan --plan tmp/ai-session-handler-template-smoke.md
ai-session-handler status --plan tmp/ai-session-handler-template-smoke.md
```

Confirm creation succeeds, status fails with the incomplete-template guidance, and a second create
attempt refuses to overwrite the file. Remove the disposable smoke-test file afterward.

### Completion criteria

- Invalid free-form plans receive actionable guidance without semantic phase guessing.
- README, AGENTS, and active format documentation agree on the command and contract.
- All Ruff, mypy, and pytest checks pass.
- The globally installed command exposes `create-plan` and passes sibling-repository acceptance.

## Phase 4: Roll Out Repository Plan-Authoring Instructions

### Goal

Add one consistent pointer to the centralized template workflow in every existing workspace
repository instruction file, without duplicating the template specification or changing unrelated
planning meanings.

### Prerequisite

Do not begin this phase until Phase 3 proves that the bare global command exposes `create-plan` from
a sibling repository. If it does not, stop rather than writing broken instructions.

### Scope

- Existing `/workspace/*/AGENTS.md` files discovered at execution time
- Repository-specific documentation only when its current planning rule would otherwise conflict
  with the new instruction

### Required context

1. Discover instruction files rather than relying on a maintained static inventory:

   ```bash
   find /workspace -mindepth 2 -maxdepth 2 -name AGENTS.md -type f -print | sort
   ```

2. Read each file before editing it and follow its repository-specific write boundaries and
   documentation rules.
3. Preserve existing instructions that already require `## Phase ...` sections; replace or augment
   them without leaving two competing format rules.
4. Preserve unrelated meanings of "plan" in personal, financial, health, home, shopping, or other
   non-software repositories.

### Canonical instruction

Use this meaning consistently, adapting only surrounding prose or command wrapping:

> When creating an implementation or execution plan intended for AI Session Handler, first run
> `ai-session-handler create-plan --plan PATH`, replace every placeholder, and retain the numbered
> `## Phase N: Title` headings.

Do not copy the full template, sentinel, parser regex, or recommended sections into each AGENTS
file. The installed command is the source of truth.

### Implementation

1. Add the canonical instruction to the nearest planning, development-workflow, or documentation
   section in every discovered repository AGENTS file.
2. In repositories without such a section, add the smallest clearly named planning subsection
   suitable for the one instruction; do not reorganize the rest of the file.
3. Keep the qualifier "intended for AI Session Handler" so ordinary design and personal planning
   documents are not accidentally converted into executable plans.
4. Do not create a new AGENTS file in a repository that does not already have one as part of this
   phase. New-repository instruction seeding is a separate workspace-template decision.
5. Preserve all unrelated dirty worktree changes. Inspect each diff independently and stop for
   clarification if the intended insertion overlaps an unresolvable user edit.

### Validation

1. Re-run discovery and verify every existing `/workspace/*/AGENTS.md` contains exactly one
   `ai-session-handler create-plan --plan` instruction.
2. Search for stale hand-authored handler format guidance and reconcile only true conflicts:

   ```bash
   rg -n "When creating plans|## Phase|create-plan" /workspace/*/AGENTS.md
   ```

3. Review `git diff -- AGENTS.md` separately in every affected repository. Confirm no unrelated
   content or pre-existing changes were altered.
4. From two repositories with materially different AGENTS files, run `ai-session-handler
   create-plan` against disposable untracked paths and verify the same template is produced. Remove
   the smoke-test files afterward.

### Completion criteria

- Every existing workspace repository AGENTS file contains exactly one concise centralized
  authoring instruction.
- Existing repository-specific planning and documentation rules remain intact.
- Non-handler planning documents are explicitly outside the instruction's scope.
- The rollout points only to a command already accepted through the global sandbox installation.
- No template content is duplicated across repositories.
