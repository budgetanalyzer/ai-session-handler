# Reliability Review Fixes

Address the three findings from the review of `astra-hardening` at `91b0607` against local
`main` at `e3a97c2`, following the implementation of
[Reliable Phase Execution and Fresh-Session Handoffs](reliability-and-session-handoffs.md).

The reviewed baseline passed 187 tests, Ruff formatting and linting, strict mypy, and diff
whitespace checks. Four temporary diagnostic probes reproduced the three findings below;
those probes were removed after review. Recreate them as permanent regression tests in the
appropriate existing test modules.

## Execution and acceptance

Implement the phases in order through separate manual development sessions in
`/workspace/ai-session-handler`. **Do not execute this plan with AI Session Handler:** it changes
the runner and bundled wrapper underneath an executing invocation. The numbered phases follow
the [canonical plan format](../plan-format.md) and serve as independently verifiable checkpoints.

Read this introduction and the selected phase at the start of each session. Inspect current
changes before editing. Record progress and validation evidence in the development conversation
or ordinary review documentation; do not edit handler-owned runtime data. Final acceptance
belongs to the user.

## Scope and constraints

- Preserve Python 3.12+, the stdlib-only runtime, strict typing, existing command options and
  exit semantics, and provider-agnostic core execution.
- Preserve the current generated layout, state shape, outcome commit ordering, explicit retry
  requirements, plan snapshots, and workspace locks. Add no migrations or compatibility paths.
- Keep fixes within this repository. Do not change the external `codex-lean` launcher or call
  real providers during validation.
- Keep the generic terminal-marker contract strict. Diagnostic normalization belongs to the
  bundled provider wrapper, and only its validated final-message file supplies a terminal result.
- Retain version `0.2.0`; the review found no version inconsistency, and another release bump is
  outside this repair plan.
- Update behavior documentation with each owning phase. Leave archived conversations untouched.

Use repository-local verification tools. If `.venv/` is missing, follow the development setup in
`AGENTS.md` and `README.md`. After each phase, run its focused tests and:

```bash
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src tests
```

Use temporary workspaces and fake processes. Synchronize lifecycle probes with pipes, events, or
explicit boundary hooks rather than timing guesses. Each subprocess test must have an outer
deadline and unconditional cleanup independent of the code under test. Use monkeypatching in the
test harness for fault injection; do not add production CLI switches or configuration for tests.

## Phase 1: Protect the complete worker lifecycle from catchable interruption

### Workspace

.

### Goal

Fix P1: SIGTERM during worker startup or cleanup must follow controlled process cleanup and
durable interruption handling instead of leaving a live worker behind.

### Scope

Signal ownership and subprocess cleanup in `src/ai_session_handler/runner.py`, the analogous
lifecycle in `src/ai_session_handler/provider_wrappers/codex_high_exec_filter.py`, focused
runner/wrapper tests, and lifecycle documentation.

### Non-goals

Cleanup after SIGKILL, container failure, or deliberate process detachment; automatic retries;
new process supervision services; or broader direct-wrapper descendant guarantees.

### Required context

Read `AGENTS.md`, `README.md`, `docs/state-and-recovery.md`, `docs/session-lifecycle.md`,
`run_phases()`, `run_agent_process()`, both `_managed_termination_signals()` implementations,
the process-group and pipe/thread cleanup helpers, `tests/test_runner.py`, and
`tests/test_codex_high_exec_filter.py`.

The review injected SIGTERM immediately after persisting the launched worker's process identity.
The handler exited with return code `-15`, the worker remained alive, and durable state retained
an unresolved running attempt. Signal handlers currently cover only the polling loop: startup
precedes their installation, and group cleanup/final draining follow their restoration.

### Execution steps

1. Add deterministic failing tests for SIGTERM at process-identity persistence and after the
   polling loop, before ordinary process-group cleanup. Include a resistant descendant and a
   leader that has already exited so cleanup cannot rely solely on leader liveness.
2. Establish main-thread signal handling before worker launch and retain it through worker
   cleanup. Ensure interruption cannot escape between child creation and recording enough local
   process identity to clean up that child. Use a small, explicit deferral mechanism where
   asynchronous exceptions would interrupt ownership acquisition or cleanup.
3. Preserve SIGINT/SIGTERM exit behavior while ensuring a catchable interruption stops ordinary
   group members before releasing workspace ownership. A further catchable signal during cleanup
   must not skip the remaining kill, pipe-close, or thread-join work. Restore prior handlers and
   any signal masks after cleanup; the worker must not inherit unintentionally blocked signals.
4. Audit partial startup: failures or signals may occur before all threads have started. Join
   only started threads and clean up opened pipes without masking the original interruption.
   Preserve bounded cleanup when stdin is blocked or output processing has failed.
5. Keep interruption recording connected to the lifecycle change. When persistence succeeds,
   record an interrupted outcome and stop; when persistence fails, retain the last durable active
   attempt and propagate the signal-driven exit. Never infer completion from captured output.
6. Apply the same signal-scope correction to direct execution of the bundled wrapper while
   keeping its child in the outer runner's process group. Preserve its existing immediate-child
   cleanup contract and provider exit code on ordinary completion.
7. Update `docs/state-and-recovery.md` and the relevant README lifecycle description to state
   when signals are managed and how interruption during startup/cleanup is handled. Retain the
   documented abrupt-termination limitations.

### Implementation notes

Moving the existing context manager around a larger block is insufficient if its handler can
raise midway through final cleanup or before the newly created process is owned locally. Keep
the signal policy and cleanup ordering explicit, without building a general lifecycle framework.
Reuse current outcome/state transitions and keep locks held through cleanup and result recording.

### Validation

```bash
.venv/bin/python -m pytest tests/test_runner.py tests/test_codex_high_exec_filter.py tests/test_cli.py
```

Cover SIGINT and SIGTERM at startup, ordinary execution, and cleanup; repeated interruption during
cleanup; partially started threads; restored signal handlers; failed interruption persistence;
and direct-wrapper child cleanup. Reuse existing timeout, stop-regex, blocked-stdin, broken-output,
and resistant-descendant tests where they already exercise the required behavior.

### Completion criteria

The startup and cleanup reproductions terminate all owned ordinary workers. Catchable
interruption records the existing interrupted stop when IO succeeds, preserves unresolved
evidence when it does not, and restores caller signal handling. Focused tests and quality gates
pass with no surviving test workers.

## Phase 2: Make wrapper diagnostics inert to terminal-result parsing

### Workspace

.

### Goal

Fix P2: useful provider diagnostics must not cause the core runner to reject an otherwise valid
authoritative final result.

### Scope

Diagnostic rendering and final-message emission in
`src/ai_session_handler/provider_wrappers/codex_high_exec_filter.py`, wrapper/acceptance tests,
`docs/worker-protocol.md`, and README wrapper documentation.

### Non-goals

Relaxing the generic parser, adding a trusted-provider mode to core execution, introducing a new
terminal protocol, changing provider settings, or implementing a general Markdown sanitizer.

### Required context

Read `README.md`, `docs/worker-protocol.md`, `docs/session-lifecycle.md`, `markers.py`, the bundled
wrapper, `tests/test_markers.py`, `tests/test_codex_high_exec_filter.py`, and
`tests/test_acceptance.py`. Preserve the lifecycle guarantees established in Phase 1.

The review reproduced two failures by passing diagnostics through `_stream_filtered_output()`
and then appending a valid final completion block:

- A diagnostic containing `<phase-blocked >` remained unsanitized and raised `InvalidMarkerError`.
- A file excerpt beginning with three backticks followed by `python`, with no closing fence,
  left the subsequent result inside the parser's fence state and raised `InvalidMarkerError`.

Exact-tag substitution currently handles neither case. Moving diagnostics to stderr alone does
not solve malformed tags because the core parser validates both streams.

### Execution steps

1. Add failing regressions for both diagnostic forms followed by a valid final-message file.
   Exercise the actual bundled wrapper against a fake `codex-lean`, then validate its captured
   streams with the core parser. Include an installed-handler acceptance case using that wrapper.
2. Use one simple diagnostic rendering path after live marker filtering: escape literal `<`
   characters as `&lt;` and prefix each visible diagnostic line with a stable `[codex] ` label.
   Escaping prevents exact and malformed tag recognition; the prefix prevents backtick and tilde
   lines from changing fence state. Track line boundaries across writes so chunking cannot
   change the result. Preserve stdout/stderr destinations and useful diagnostic text.
3. Apply that rendering path to ordinary live output, filter EOF flushes, and invalid final-message
   diagnostics. Continue hiding complete live marker blocks. Handle CRLF and an unfinished final
   line, and finish diagnostics before emitting a separate, unprefixed terminal-result line.
4. Validate the raw authoritative final-message file with the existing strict parser before
   normalization. Emit only its validated result, preserving the result body. Missing or invalid
   final content must remain a marker failure even if live output includes a completion block.
   Preserve nonzero provider exit behavior and the runner's failure precedence.
5. Document the diagnostic label/escaping and the fact that wrapper transcripts contain normalized
   diagnostics. Keep the core's reserved-tag, fence, independent-stream, and exactly-one-result
   rules intact. Update wrapper tests that assert the former diagnostic representation.

### Implementation notes

Keep normalization at the wrapper boundary. Do not escape the raw final message before validating
it, because that could hide malformed protocol content. Test all three outcome kinds and both
output streams. The new diagnostic representation must not turn failure prose or a logged example
into an authoritative result.

### Validation

```bash
.venv/bin/python -m pytest tests/test_codex_high_exec_filter.py tests/test_markers.py tests/test_acceptance.py
```

Cover malformed and truncated tags, exact live blocks, unclosed backtick/tilde excerpts, tags
split across writes, multiline output, CRLF, EOF without a newline, invalid final responses,
provider nonzero exit, and complete/blocked/clarification final results. Assert useful diagnostic
content remains visible and that raw misleading output from generic commands still fails.

### Completion criteria

Both wrapper reproductions accept the valid authoritative result through core execution.
Malformed or absent authoritative results still fail. Generic marker tests retain their strict
behavior, lifecycle tests remain green, and the diagnostic representation is documented.

## Phase 3: Report the unresolved attempt and verify the integrated repairs

### Workspace

.

### Goal

Fix P2: after a persistence failure, the CLI must direct the operator to the current unresolved
attempt's artifacts. Complete integrated verification of all three repairs.

### Scope

`_print_run_outcome()` in `src/ai_session_handler/cli.py`, focused CLI/failure tests, recovery
documentation, and the final verification handoff.

### Non-goals

Changing state transitions, adopting uncommitted outcomes, retrying failed writes, or adding
an approval workflow or another release version.

### Required context

Read `README.md`, `docs/state-and-recovery.md`, `_print_run_outcome()`, runner outcome/state-write
failure branches, `tests/test_cli.py`, `tests/test_runner.py`, and the changes from Phases 1–2.

The review completed phase 1 and injected an outcome-write failure in phase 2. Durable state
correctly retained phase 2's active attempt, but the CLI printed phase 1's outcome and transcript
because it checked `last_run` first. Phase 2's transcript path was absent from the error output.

### Execution steps

1. Add a two-phase CLI regression that succeeds in phase 1 and fails phase 2's outcome write.
   Add the corresponding case where the outcome file is written but state replacement fails.
   Assert the retained active attempt and the artifact paths printed to stderr.
2. Prefer `active_attempt` when an execution failure leaves unresolved work; report its phase/id,
   transcript path, and transcript tail. If displaying its planned outcome path, explicitly label
   it uncommitted and do not imply the file exists. Use `last_run` when no active attempt remains.
3. Keep the failure message and exit code 4. If the transcript was never created or cannot be read,
   report the current path and read failure without substituting the preceding run's artifacts.
   Preserve ordinary committed-failure reporting and existing quiet-mode behavior.
4. Update recovery documentation and the README failure-reporting description to explain artifact
   selection for unresolved work. Reporting must not mutate state or commit an outcome reference.
5. Run the complete quality gates, inspect the combined diff for scope and documentation agreement,
   and confirm each review finding has a permanent owning regression. Reuse existing installed
   entrypoint, concurrency, retry, snapshot, and handoff coverage; add only missing integration
   checks exposed by the repairs.
6. Provide a review handoff listing the fixes, exact validation results, and remaining documented
   process/persistence limitations. Confirm version consistency remains `0.2.0` and leave final
   acceptance to the user.

### Implementation notes

The preserved `active_attempt` is the intended recovery evidence, not a state error to clear.
Keep this repair in presentation code unless a regression exposes a necessary correction to the
existing failure transition. Avoid presenting an earlier committed success as the failed attempt.

### Validation

Run focused CLI and runner tests during implementation, then the complete checks:

```bash
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src tests
.venv/bin/python -m pytest
git diff --check
.venv/bin/ai-session-handler --version
```

Cover outcome-write failure, state-commit failure, failure before transcript creation, and ordinary
committed agent failure with previous history present. Assert correct current-artifact reporting
and conservative recovery. Confirm subprocess fixtures leave no live workers and documentation
examples agree with the final implementation.

### Completion criteria

Persistence failures identify the unresolved attempt and its transcript, with no misleading
previous-phase artifacts. All three review findings have passing regression coverage, the full
quality gates pass, and the changes and evidence are ready for the user's review.
