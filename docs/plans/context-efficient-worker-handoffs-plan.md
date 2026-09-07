# Context-Efficient Worker Handoffs Plan

Reduce known prompt and discovery duplication without imposing arbitrary size limits or adding
provider usage instrumentation. Preserve the accepted plan's exact global preamble and selected
phase body, keep durable outcome discovery, and make generated prompts explicit that their embedded
plan snapshot is authoritative for the selected phase. Detailed plan-wide background should live in
ordinary repository documentation and be referenced from a concise preamble, but the runner must
not enforce a size threshold, estimate tokens, or persist prompt/provider metrics.

## Phase 1: Remove Duplicate Handoff Content and Redundant Discovery

### Workspace

.

### Goal

Make each fresh-worker prompt contain one copy of the latest terminal summary and direct workers to
use the exact embedded plan snapshot before rereading source plans, outcomes, or transcripts.

### Scope

- Make `DURABLE HANDOFF CONTEXT` the only prompt section containing the latest terminal summary.
- Retain last-run identity, status, timestamps, exit code, execution workspace, and artifact paths
  in `PREVIOUS STATE SUMMARY` without repeating summary prose.
- Preserve the exact global preamble, exact selected phase body, and complete phase/status/path
  index of earlier committed outcomes.
- Clarify worker instructions that the embedded global preamble and selected phase body come from
  the accepted immutable snapshot and are sufficient for ordinary phase startup.
- Tell workers to open prior outcome records only when a concrete earlier decision, limitation, or
  validation result is relevant, and not to replay transcripts by default.
- Update plan-authoring guidance to favor concise plan-wide intent plus links to durable repository
  documentation, without enforcing a byte, character, token, line, or section-count limit.
- Update prompt fixtures, focused tests, and all documentation that describes fresh-worker context.

### Non-goals

- Do not reject, warn about, truncate, summarize, normalize, or rewrite a plan based on preamble or
  prompt size.
- Do not count or persist prompt bytes, characters, lines, estimated tokens, provider-reported
  tokens, cache usage, context percentages, or billing data.
- Do not add a provider usage protocol, usage artifact, CLI metric, state field, outcome field,
  configuration option, runtime dependency, or provider adapter.
- Do not remove global plan intent, selected phase instructions, the latest terminal summary, or
  discoverability of earlier committed outcomes.
- Do not prevent a worker from reading the source plan, an outcome, or a transcript when repository
  reality, recovery investigation, or a specific handoff dependency makes that evidence necessary.
- Do not change process execution, terminal marker parsing, state transitions, generated layouts,
  or release compatibility behavior.

### Required context

- `AGENTS.md`
- `README.md`, especially prompt generation, generated artifacts, retries, and final review
- `docs/plan-format.md`
- `docs/session-lifecycle.md`
- `docs/state-and-recovery.md`
- `docs/worker-protocol.md`
- `src/ai_session_handler/prompts.py`
- `tests/test_prompts.py`
- `tests/fixtures/prompts/worker_prompt.txt`
- `tests/test_acceptance.py`

### Execution steps

1. Remove `last_run.summary` from `summarize_previous_state` while retaining every non-summary
   last-run field. Keep the summary under `latest_relevant_summary` in
   `summarize_handoff_context`.
2. Add focused prompt tests proving that a multiline latest summary appears exactly once, the
   latest outcome is not duplicated in the earlier-outcome index, and completed, blocked,
   clarification, and failed outcome references retain their status labels and paths.
3. Revise the worker prompt instructions to state that `GLOBAL PLAN INTENT` and
   `SELECTED PHASE BODY` are exact copies from the invocation's accepted snapshot. Direct the
   worker to use those sections for ordinary startup rather than rereading the complete source
   plan.
4. Narrow the prior-history instruction: inspect an indexed outcome only when its concrete
   decision, validation evidence, limitation, or recovery history affects the selected phase. State
   that complete transcripts are diagnostic evidence and should not be replayed by default.
5. Keep escape hatches explicit. A worker may inspect source plan bytes when diagnosing a mismatch
   or repository contradiction and may inspect outcomes or transcripts when the selected phase or
   recovery evidence requires them.
6. Update the deterministic worker prompt fixture and review the entire diff to ensure the global
   preamble, selected phase body, outcome index, marker grammar, and workspace restrictions remain
   intact.
7. Update `README.md`, `docs/plan-format.md`, `docs/session-lifecycle.md`,
   `docs/state-and-recovery.md`, and `docs/worker-protocol.md` with the same context hierarchy:
   concise preamble and durable-document references when authoring; exact embedded snapshot for
   execution; latest summary once; outcomes on demand; transcripts for diagnostics rather than
   routine handoff replay.
8. Add or adjust an installed-entrypoint acceptance assertion showing that handoffs remain usable
   across retries while the latest summary occurs once and earlier outcomes remain discoverable.
9. Run focused validation, then the complete repository quality gates, and inspect the final diff
   for unrelated changes or accidental schema, CLI, layout, or provider-specific behavior.

### Implementation notes

- This is a prompt-content and documentation change only. Keep `RunnerState`, `OutcomeRecord`,
  `ProcessResult`, transcript headers, generated paths, and command templates unchanged.
- Do not introduce a general prompt optimization abstraction. Direct edits to prompt rendering and
  its tests are sufficient.
- Exact preamble preservation remains important because plan-wide safety, scope, and acceptance
  constraints may appear there. Concision is authoring guidance, not a parser invariant.
- The committed-outcome index remains intentionally complete and status-labelled. Its paths let a
  fresh worker find durable evidence without embedding every outcome body.
- Do not tell workers never to inspect prior artifacts. The goal is to remove automatic or habitual
  replay, not to hide evidence needed for a specific phase or interruption investigation.
- Keep provider behavior outside the core. Comparative token or cache analysis remains an external
  evaluation activity using provider-owned reporting, as described in
  `docs/session-lifecycle.md`.

### Validation

Run:

```bash
.venv/bin/python -m pytest tests/test_prompts.py tests/test_acceptance.py
.venv/bin/python -m ruff format .
.venv/bin/python -m ruff check . --fix
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src tests
.venv/bin/python -m pytest
```

Review the rendered prompt fixture directly. Confirm the latest summary appears once, exact plan
sections and all earlier outcome references remain available, and no generated schema or CLI output
changed.

### Completion criteria

- Every fresh worker receives the exact accepted global preamble and selected phase body.
- The latest terminal summary appears exactly once in the rendered prompt.
- Earlier committed outcomes remain discoverable by phase, status, and path without embedding their
  bodies or replaying transcripts.
- Worker and authoring guidance discourages redundant full-plan and history reads while preserving
  evidence-driven exceptions.
- No limits, metrics, usage protocol, provider parsing, state or outcome fields, CLI options,
  dependencies, migrations, or compatibility paths are added.
- Focused tests and the complete Ruff, mypy, and pytest gates pass.
