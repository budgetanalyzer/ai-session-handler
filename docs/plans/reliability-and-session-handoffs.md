# Reliable Phase Execution and Fresh-Session Handoffs

Harden AI Session Handler's process ownership, restart behavior, execution history, input
boundaries, and worker context while retaining its small, container-local, provider-agnostic
architecture. This plan follows the codebase review and its fake-worker reproductions.

## Execution and acceptance

Implement this plan manually through separate development sessions in
`/workspace/ai-session-handler`. **Do not execute this plan with AI Session Handler:** it changes
the runner, protocol, and generated-state layout underneath an executing invocation. Numbered
phases are implementation checkpoints for the human and implementation agent.

Execute phases in order. Every phase uses this repository as its only execution workspace. Read
this introduction as well as the selected phase; its decisions apply to every phase. Record
progress and handoff evidence in the development conversation or an ordinary review document,
not in handler-owned runtime state. Inspect the current changes before resuming a phase.

Final acceptance belongs to the user. Workers must still run their phase's validation and report
evidence, but this change adds no automated reviewer, verification-command option, approval
command, or persisted user-approval state. `runner-complete` means all phases reported completion;
it does not mean the user has reviewed or accepted the resulting implementation.

## Design decisions and boundaries

- Preserve Python 3.12+, stdlib-only runtime dependencies, typed dataclasses, strict mypy, Ruff,
  pytest, atomic state writes, and `shell=False` command execution.
- Keep workspace inference from the plan and run all remaining phases by default. Do not add
  workspace/state selectors, provider adapters to core logic, a daemon, automatic retries,
  parallel execution scheduling, or git workflow operations.
- Target the existing Linux container. Use standard POSIX process groups and advisory locks;
  do not build a portability abstraction for other operating systems.
- Keep source plans as intent. Generated state, prompts, transcripts, and outcome records remain
  under the plan workspace's `.ai-session-handler/` directory. Execution-workspace coordination
  may use a lock on an existing directory descriptor without creating artifacts there.
- Use a canonical plan path, a path-derived plan key, and a unique attempt id. Content hashes
  detect edits; they do not identify distinct plans with identical contents.
- Preserve the three terminal outcome kinds. Tighten their framing and reject ambiguous output.
  A reported result remains a worker assertion; the user supplies the final semantic review.
- Make interruption and unknown outcomes explicit. A process crash cannot promise exactly-once
  file edits. Recovery requires inspection of partial changes and explicit retry after workers
  have stopped; never infer success from a transcript after an interrupted state write.
- Include global plan intent in every prompt. Preserve earlier handoff evidence without copying
  all earlier transcripts into every session or asking an LLM to summarize runner state.
- Keep optional provider configuration outside core logic. Do not edit `/usr/local/bin/codex-lean`
  or another repository in this plan. Document that fresh sessions and restricted tool access
  are separate decisions.
- Existing generated history must not be deleted, silently reassigned, or silently ignored.
  Use an explicit, documented manual transition for legacy layout/schema data; do not build a
  general migration subsystem. Fresh installations must work without migration steps.

## Validation discipline

Use repository-local tools. If `.venv/` is absent, create it with the supported Python runtime and
install `.[dev]` as described in `AGENTS.md`. For each implementation phase, run its focused tests
and these checks on the resulting repository:

```bash
.venv/bin/python -m ruff format .
.venv/bin/python -m ruff check . --fix
.venv/bin/python -m mypy src tests
```

Use fake commands and temporary repositories. Synchronize lifecycle tests with pipes, events,
or explicit readiness signals; use short sleeps only for timeout behavior. Give each subprocess
test an outer deadline and unconditional cleanup so a failed assertion cannot leave workers
behind. Do not call real providers or run live tasks against this repository during validation.

Documentation belongs to the phase that changes the behavior. Before any implementation phase
edits `AGENTS.md`, read and apply the linked
[AGENTS.md checkstyle](https://github.com/budgetanalyzer/orchestration/blob/main/docs/agents-md-checkstyle.md).
The final documentation phase checks consistency rather than postponing these updates.

## Phase 1: Resolve CLI paths and validate command templates

### Workspace

.

### Goal

Make ordinary CLI paths and malformed command templates behave predictably before execution.

### Scope

`cli.py`, command-template handling in `runner.py`, focused CLI/runner tests, and README usage.

### Non-goals

Changing workspace discovery markers, adding path selectors, or changing the worker protocol.

### Required context

Read `AGENTS.md`, `README.md`, `cli.py`, `runner.py`, `tests/test_cli.py`, and template tests in
`tests/test_runner.py`. The review reproduced subdirectory-relative paths resolving at the root,
an uncaught `IndexError` for `{}`, and an uncaught `KeyError` for nested format fields.

### Execution steps

1. Resolve `--plan` once against the caller's current directory and infer the workspace from that
   canonical path. Share this flow between `run` and `status`.
2. Restrict templates to the documented named fields and literal escaped braces. Reject positional
   fields, attribute/index access, conversions, format specifications, and malformed quoting with
   a specific `CommandTemplateError`.
3. Validate templates before recording a running phase or launching a worker. Preserve argument
   boundaries by splitting the template before replacing path values inside arguments.
4. Convert expected input failures to exit code 5 without tracebacks or execution-state mutation.
   Update command-template and relative-path examples in README.

### Implementation notes

Paths containing spaces or quotes must remain one argument. Preserve literal braces intended for
the invoked command. Do not broaden the CLI exception handler to hide programming errors.

### Validation

Run `.venv/bin/python -m pytest tests/test_cli.py tests/test_runner.py`. Cover root/subdirectory
invocations, sibling repositories reached by relative paths, absolute paths, spaces, literal
braces, empty commands, and each rejected formatting construct. Assert invalid templates do not
launch a sentinel worker or create attempt state.

### Completion criteria

Both commands address the same supplied plan regardless of caller directory. Template errors
name the invalid syntax, preserve existing state, and return the documented input-error code.

## Phase 2: Parse immutable plan snapshots and detect edits between phases

### Workspace

.

### Goal

Execute only actual Markdown phases from the accepted plan snapshot.

### Scope

Plan parsing, plan-hash boundaries, associated typed values, tests, and `docs/plan-format.md`.

### Non-goals

A general Markdown renderer, live plan reloading, or automatically accepting edits.

### Required context

Read `phases.py`, plan-identity functions in `state.py`, the phase loop in `runner.py`, and
`tests/test_phases.py` / `tests/test_state.py`. The review reproduced a fenced example becoming
an executable phase and an edited second phase running with stale instructions.

### Execution steps

1. Introduce a small typed plan snapshot containing canonical path, hash of the read bytes,
   global preamble text, and parsed phases. Parse and hash the same read, preserving phase bodies
   and line numbers, including CRLF input.
2. Ignore headings inside backtick and tilde fences when finding phases or workspace sections.
   Keep the supported Markdown subset explicit and return path/line errors for malformed plans.
3. Use the snapshot for initial acceptance and selection. Recheck the source against the accepted
   snapshot before subsequent launches and before declaring the invocation fully complete.
4. On an edit, retain recorded outcomes but stop advancement with a hash-mismatch diagnostic and
   exit code 5. Apply `--accept-plan-change` only at invocation initialization, retaining completed
   phase-id checks. Update the plan-format and README contracts.

### Implementation notes

The runner does not continuously watch files. A change during a worker's execution is detected
at the next checkpoint; the completed worker's outcome refers to the original snapshot.
Global preamble capture prepares Phase 8 without copying later phases into worker prompts.

### Validation

Run `.venv/bin/python -m pytest tests/test_phases.py tests/test_state.py tests/test_runner.py
tests/test_prompts.py tests/test_cli.py` as one command. Cover fenced examples, workspace-like
headings in fences, exact body preservation, invalid UTF-8 diagnostics, edits during a fake
worker, and an edit during the last phase. Verify no subsequent worker launches after mismatch.

### Completion criteria

The hash describes exactly the parsed bytes. Examples are not execution units, and an invocation
cannot report full completion while knowingly holding an outdated accepted plan.

## Phase 3: Separate plan identity from attempt identity

### Workspace

.

### Goal

Prevent state aliasing and preserve every attempt's artifacts.

### Scope

Generated paths, plan-path identity checks, run ids, exclusive artifact creation, and layout docs.

### Non-goals

Schema redesign, automatic history migration, or changing the plan workspace's ownership role.

### Required context

Read `config.py`, `state.py`, `transcripts.py`, prompt file writing, and `create_run_id()`. Account
for equal-stem plans in different directories, `config.md`, and retries within one second.

### Execution steps

1. Derive a stable plan key from the canonical workspace-relative plan path using stdlib hashing.
   Store new plan state at `.ai-session-handler/plans/<plan-key>/state.json`. Keep shared config
   at `.ai-session-handler/config.json` and validate stored plan path independently of content hash.
2. Give each attempt a UUID-based unique id, optionally retaining timestamp/phase text for humans.
   Keep its prompt and transcript under the owning plan directory and create them exclusively.
3. Update init/status/error reporting and tests to use the layout through direct path helpers.
   Do not overwrite an existing artifact even if a test forces a duplicate id.
4. Detect legacy stem-based state before treating a plan as new. Return a specific transition
   message, preserve old artifacts, and document manual backup/relocation and identity checks.
   Document that renaming a plan requires an explicit history decision.

### Implementation notes

Keep the layout conventional with no CLI customization. A legacy state-shaped `config.json`
must produce a specific collision diagnostic, rather than becoming an empty/default config.
Create `docs/state-and-recovery.md` to document layout and transition instructions.

### Validation

Run `.venv/bin/python -m pytest tests/test_state.py tests/test_runner.py tests/test_cli.py
tests/test_prompts.py` as one command. Test same-stem/same-content plans, `config.md`, path aliases,
same-second attempts, forced id collisions, and legacy-state detection without file modification.

### Completion criteria

Distinct plans cannot share completion history accidentally. Every attempt retains distinct
artifacts, and legacy history cannot be silently skipped or destroyed.

## Phase 4: Own and clean up worker process groups

### Workspace

.

### Goal

Ensure controlled stops and execution exceptions stop the worker's ordinary process tree.

### Scope

Subprocess lifecycle, streams/threads, wrapper cleanup, fake-process tests, and lifecycle docs.

### Non-goals

OS sandboxing, control of deliberately detached processes, or durable interruption recovery.

### Required context

Read `run_agent_process()`, `_terminate_process()`, transcript IO, and
`provider_wrappers/codex_high_exec_filter.py`. The review reproduced surviving descendants after
a timeout. Read the Phase 3 layout before adjusting artifact opening order.

### Execution steps

1. Prepare/open artifacts before launching where possible and start each worker in a new POSIX
   session/process group. Keep the provider wrapper's child in that group.
2. Terminate the entire group on timeout or stop-regex, allow a short grace period, then kill
   remaining group members even if the original group leader has already exited.
3. Add lifecycle `try/finally` cleanup covering stdout/stderr failures, transcript errors,
   interruption, and exceptional exits. Close pipes and finish reader/writer threads predictably.
4. Give direct wrapper execution appropriate child cleanup as well, without creating a group
   that escapes handler ownership. Preserve provider exit behavior on ordinary completion.
5. Document the process-group guarantee and its limits in `docs/state-and-recovery.md` and README.

### Implementation notes

Signal handlers belong at an appropriate main-thread boundary and must be restored after use.
Do not promise cleanup after SIGKILL or against intentional daemonization. Never signal the
handler's own process group. A leader exit alone does not establish that all descendants stopped.

### Validation

Run `.venv/bin/python -m pytest tests/test_runner.py tests/test_codex_high_exec_filter.py
tests/test_cli.py` as one command. Cover wrapper/child/grandchild trees, a descendant ignoring
SIGTERM, a leader exiting first, blocked stdin, broken output sinks, and transcript IO failures.
Use outer deadlines and cleanup independent of the runner being tested.

### Completion criteria

Controlled stops terminate ordinary descendants, including resistant children. IO exceptions do
not abandon workers, and successful streaming and provider exit-code behavior remain covered.

## Phase 5: Establish exclusive execution ownership

### Workspace

.

### Goal

Prevent cooperating handler invocations from running overlapping writers.

### Scope

Invocation ownership, execution-workspace locking, contention diagnostics, and concurrency tests.

### Non-goals

Parallel phase scheduling, queues, blocking lock waits, or excluding unrelated editors/tools.

### Required context

Read the phase loop, Phase 4 cleanup, and `docs/state-and-recovery.md`. Atomic JSON replacement
alone does not serialize readers and writers. Different plan repositories may target the same
execution workspace.

### Execution steps

1. Use nonblocking stdlib `fcntl.flock` ownership before reading mutable execution state. Serialize
   invocations within a plan workspace; fail promptly with exit code 5 on contention.
2. Lock the selected canonical execution directory before recording/launching that phase, using
   an existing directory descriptor to avoid creating generated files in another repository.
   Reuse ownership when plan and execution workspaces are the same directory.
3. Hold locks through process cleanup and durable result recording. Release them on every exit.
   Keep descriptors out of unrelated worker commands and handle alias paths consistently.
4. Document lock scope, nonblocking behavior, and the distinction between a released OS lock and
   an unresolved attempt after a crash. Add deterministic competing-invocation tests.

### Implementation notes

Directory descriptors are a Linux-container convention here. Do not use a persistent PID file
as proof of ownership or delete/recreate a locked path. Acquire execution ownership before
clearing a stored stop so contention cannot consume the user's retry intervention.

### Validation

Run `.venv/bin/python -m pytest tests/test_runner.py tests/test_cli.py`. Verify a second invocation
cannot launch the same phase, separate plans in one plan workspace serialize, and plans from
different roots cannot concurrently target the same execution directory. Test lock release on
normal failure and handler termination, and successful execution for independent workspaces.

### Completion criteria

Competing handler invocations fail before launching overlapping workers or altering attempt
state. Locks and subprocesses are released without polling loops or deadlocks.

## Phase 6: Persist active attempts and fail closed after interruption

### Workspace

.

### Goal

Make incomplete execution distinguishable from an untouched phase and safe to inspect/retry.

### Scope

State schema version 2, typed statuses, active-attempt transitions, CLI status, and recovery docs.

### Non-goals

Exactly-once file edits, automatic replay, inferred completion, or an automatic migration engine.

### Required context

Read `state.py`, CLI error/status boundaries, and the ownership/cleanup contracts from Phases 4–5.
The review reproduced `current_phase` with no stop or attempt metadata after interruption, followed
by a normal invocation launching a duplicate worker.

### Execution steps

1. Define schema version 2 with an explicit active attempt: id, phase, accepted snapshot identity,
   execution workspace, start timestamp, prompt/transcript paths, and process identity when known.
   Replace unconstrained persisted status strings with precise enums/literals.
2. Atomically persist the prepared attempt before launch and process identity after launch.
   Convert ordinary completion, blocked/clarification, launch failure, controlled interruption,
   and execution IO failure into explicit outcomes. Launch failures after preparation return 4;
   invalid inputs rejected before preparation retain exit code 5.
3. Treat an abandoned active attempt as interrupted/unknown. Refuse ordinary advancement and
   require inspection plus `--retry-stopped`. A retry must not proceed while a positively identified
   old worker remains alive. Never kill a process based solely on a stale numeric PID.
4. Validate persisted references and state relationships at load/selection boundaries. Make
   `status` read-only and able to report active/abandoned work and its artifacts without a traceback.
   Preserve diagnostics even when the source plan has changed.
5. Document transitions, exit codes, the launch-to-PID-record crash window, and the manual legacy
   schema transition. Preserve the last durable attempt if recording a failure itself fails.

### Implementation notes

Catchable interruption should clean up and record an interrupted stop; uncatchable termination
leaves an active record for recovery. Unknown process identity requires human cleanup and
explicit retry, not guessed liveness or success. State conversion instructions must preserve
completed ids, stop information, timestamps, and artifact references; old `current_phase` entries
must be treated conservatively. Schema errors must identify the file and relevant key.

### Validation

Run `.venv/bin/python -m pytest tests/test_state.py tests/test_runner.py tests/test_cli.py`.
Exercise SIGINT/SIGTERM, SIGKILL after readiness, missing executables, IO failures, stale/invalid
references, and failure between result capture and state replacement. Verify ordinary rerun
refusal, read-only status, explicit retry after cleanup, and retained clarification requests.

### Completion criteria

An interrupted attempt retains enough information for inspection and cannot be silently rerun
as untouched work. Normal outcomes and recovery transitions remain typed and durably recorded.

## Phase 7: Require an unambiguous final worker result

### Workspace

.

### Goal

Prevent quoted examples and diagnostic fragments from advancing phase state.

### Scope

Marker framing/parser/filter, prompt protocol, bundled wrapper, tests, and protocol documentation.

### Non-goals

Independent semantic verification, provider event parsing in core, or an approval state.

### Required context

Read `markers.py`, `prompts.py`, the bundled wrapper, and their tests. The review reproduced an
inline fixture marker followed by failure prose parsing as completion.

### Execution steps

1. Retain existing tag names, but require one nonempty result block beginning at a line boundary
   and ending as the final non-whitespace content of its emitting stream. Reject extra recognized
   blocks, nesting, malformed/unclosed recognized tags, and result-like examples in fenced text.
2. Preserve stdout/stderr identity when validating the combined captured evidence. Reject a
   result on both streams; do not require an arbitrary total ordering between independent pipes.
   Exit failure, timeout, and controlled-stop reasons always override a completion marker.
3. Update the live filter for split tags and multiline blocks. In the bundled wrapper, sanitize
   diagnostic marker text and emit only the result validated from the final-message file.
4. Update prompts, README, tests, and `docs/worker-protocol.md` together. Document wrapper obligations
   and that a well-framed false assertion is still subject to the user's manual review.

### Implementation notes

Continue accepting a final marker on stdout or stderr for generic commands. Cross-stream log
interleaving must not manufacture a marker body or invalidate an otherwise valid final result.
Keep the generic protocol textual; a provider wrapper may normalize richer output externally.

### Validation

Run `.venv/bin/python -m pytest tests/test_markers.py tests/test_prompts.py tests/test_runner.py
tests/test_codex_high_exec_filter.py tests/test_cli.py` as one command. Cover logged fixtures,
trailing failure prose, empty/nested/truncated/duplicate tags, split reads, multiline summaries,
stderr diagnostics, and all three legitimate outcomes.

### Completion criteria

The reproduced fixture cannot complete a phase. Normalized final results work, ambiguous output
fails closed, and no new automated review or approval mechanism is introduced.

## Phase 8: Carry global intent and durable handoffs into fresh sessions

### Workspace

.

### Goal

Let a fresh worker recover relevant intent, prior decisions, and verification evidence.

### Scope

Prompt context, per-attempt outcome records, state links, prompt fixtures, and handoff docs.

### Non-goals

LLM-generated compaction, a memory service, replaying complete transcripts, or worker-written state.

### Required context

Read the snapshot type from Phase 2, layout from Phase 3, state transitions from Phase 6, and
protocol from Phase 7. The existing prompt omits the plan introduction and automatically includes
only the most recent free-form worker summary.

### Execution steps

1. Include the accepted plan preamble in a clearly delimited global-intent section of every worker
   prompt, alongside the selected phase body preserved exactly. Require reading referenced
   context and restrict execution to the selected workspace and phase.
2. Ask the terminal summary to name changed artifacts, validation commands and results, decisions,
   remaining limitations, and relevant handoff references. Retain plain text as the outcome body;
   do not add brittle parsing of headings inside the summary.
3. Persist a versioned, runner-owned outcome JSON record per attempt in its plan directory,
   including plan/phase identity, status, summary, timestamps, and artifact paths. Link committed
   outcomes from state; write the record before advancing state and never adopt an unreferenced
   record as proof of success after a crash.
4. Include the latest relevant summary and a compact index of earlier committed outcome paths in
   prompts. Instruct workers to read relevant prior decisions and store durable design context
   in the ordinary repository documentation when required by their phase.
5. Document how a user supplies clarification through durable plan/context edits and accepts a
   changed plan before retry. Update `docs/worker-protocol.md`, state docs, and README examples.

### Implementation notes

Keep raw output in transcripts. Earlier history should be discoverable without inlining every
summary into the prompt. Clearly identify prior failed attempts so their partial work is not
presented as a completed prerequisite. The runner alone creates and commits outcome records.

### Validation

Run `.venv/bin/python -m pytest tests/test_prompts.py tests/test_state.py tests/test_runner.py
tests/test_cli.py` as one command. Use a three-phase fake plan with an introductory constraint,
an early decision needed later, a clarification/retry, and injected failure between outcome and
state writes. Verify context availability and conservative recovery without transcript replay.

### Completion criteria

Every phase receives global intent, earlier handoffs remain reachable after later phases, and
history stays runner-owned and cannot imply success before the state transition commits.

## Phase 9: Keep output processing responsive under verbose workers

### Workspace

.

### Goal

Prevent output handling from starving timeout checks or retaining unnecessary duplicate logs.

### Scope

Stream readers, queue draining, marker accumulation, regex evaluation, and focused stress tests.

### Non-goals

A regex engine, transcript truncation, new output-limit configuration, or changing regex meaning.

### Required context

Read output handling in `runner.py` and the framing/filter contract from Phase 7. The original
implementation uses an unbounded queue, reads whole lines, and repeatedly scans accumulated
output even when no new output has arrived.

### Execution steps

1. Read bounded chunks and bound the producer queue; ensure readers can exit during cleanup even
   if consumers fail while the queue is full. Preserve incremental UTF-8 decoding and stream id.
2. Bound work drained per loop iteration so a constantly verbose process cannot postpone timeout
   and stop handling indefinitely. Flush final captured output before outcome validation.
3. Track marker framing incrementally with the Phase 7 rules and use transcripts as the durable
   log. Avoid retaining the complete diagnostic output in the ordinary no-regex path solely for
   marker parsing; keep only the protocol state and result text needed for validation.
4. Evaluate stop regexes only after new output and after the final drain. Preserve full-history
   matching semantics when regexes are configured, and explicitly document their memory/CPU
   cost rather than silently switching to a sliding window or claiming bounded regex execution.

### Implementation notes

This is targeted output-loop hardening, not a new telemetry system. Arbitrarily large result
summaries and arbitrary stdlib regular expressions still have costs; describe the limits honestly.
Keep quiet mode and complete transcripts intact.

### Validation

Run `.venv/bin/python -m pytest tests/test_runner.py tests/test_markers.py
tests/test_codex_high_exec_filter.py tests/test_cli.py` as one command. Cover sustained output,
very long lines, split Unicode/tags, blocked queues during cleanup, a regex match in the final
chunk, and a noisy worker that must still time out. Assert transcript completeness on normal exit.

### Completion criteria

Output floods do not starve lifecycle handling, ordinary diagnostics are not duplicated forever
in memory, and regex/transcript behavior remains documented and regression-tested.

## Phase 10: Align planning guidance, architecture docs, and repository hygiene

### Workspace

.

### Goal

Describe the actual guarantees and base phase sizing on useful checkpoints.

### Scope

README, `docs/plan-format.md`, new operational/protocol docs, historical conversation labels,
`AGENTS.md`, and `.gitignore`.

### Non-goals

Changing the external launcher, enabling subagents automatically, running model benchmarks,
adding dependencies, or adding final-review enforcement to the runner.

### Required context

Read the completed implementation and docs from Phases 1–9 and the historical architecture
conversation. Read the AGENTS.md checkstyle before editing agent instructions. If documenting
current Codex behavior, apply the OpenAI Docs skill and verify current official sources.

### Execution steps

1. Make coherent, independently verifiable checkpoints the primary phase-sizing rule. Describe
   existing percentages and time estimates as provisional heuristics, not measured limits or
   universal quality thresholds. Preserve one execution repository per phase.
2. Explain the distinctions between a fresh process/conversation, cwd/filesystem confinement,
   worker-reported completion/user acceptance, and atomic persistence/exactly-once execution.
   Document manual final review as the intended acceptance workflow.
3. Explain that native subagents and compaction can complement fresh phases. State that wrapper
   tool restrictions are separate configuration choices; document the external `codex-lean`
   dependency without modifying it or silently changing permissions/model defaults.
4. Align AGENTS.md with new lifecycle, layout, state, protocol, and handoff contracts. Label the
   archived walkthrough as historical and link to current documentation without rewriting the
   conversation. Remove stale active examples.
5. Replace unrelated Java/React ignore rules with the repository's Python/generated-artifact
   rules and appropriate local-secret/editor exclusions. Preserve all required Python ignores.

### Implementation notes

Add a short optional manual evaluation recipe holding model, tools, and permissions constant
across fresh phases, continuation/compaction, and native delegation. Measure accepted correctness,
human interventions, repeated discovery, elapsed time, and token/cache usage. Executing that
experiment is separate future work and is not required for this implementation's acceptance.

### Validation

Check relative Markdown links and CLI examples against the implementation. Use `git check-ignore`
with representative Python caches, generated state, and ordinary source/docs paths. Run
`.venv/bin/python -m pytest tests/test_cli.py tests/test_prompts.py` for documented interfaces.
Confirm no example instructs executing this implementation plan with the handler.

### Completion criteria

Active documentation agrees with behavior and user-owned final acceptance. Historical material
is clearly labeled, and fresh-session guidance makes no unsupported performance guarantees.

## Phase 11: Verify the integrated workflow and prepare the user's final review

### Workspace

.

### Goal

Deliver evidence that the changes work together and a concrete result for manual acceptance.

### Scope

Cross-module fake-worker acceptance coverage, full quality gates, and a concise review handoff.

### Non-goals

Running this plan through the handler, live provider tests, automatic approval, or broad refactors.

### Required context

Read this entire plan, final source changes, updated docs, and validation evidence from earlier
phases. Revisit all review reproductions and verify each has an owning regression test.

### Execution steps

1. Exercise an installed-entrypoint fake-worker workflow across temporary repositories: completion,
   blocking, clarification, explicit retry, accepted plan edits, distinct plan identities, and
   preserved prompts/transcripts/outcomes. Running temporary fake plans is allowed; running this
   implementation plan through the handler is not.
2. Verify concurrency refusal, descendant cleanup, abandoned-attempt recovery, malformed input,
   and misleading marker output through the CLI. Reuse earlier tests where they already cover
   the integrated path; add only missing acceptance coverage.
3. Run the complete quality gates and inspect the final diff for scope, documentation accuracy,
   accidental runtime dependencies, generated artifacts, and changes outside this workspace.
4. Provide the user with a review handoff listing behavior changes, test evidence, legacy-state
   transition instructions, remaining limitations, and any decisions requiring attention. The user
   reviews the implementation and determines acceptance; do not declare user approval on their behalf.

### Implementation notes

The final user review is a human workflow step, not a handler phase status or a feature to build.
No worker assertion or passing test suite substitutes for that review. Stop adding implementation
work once the scoped changes and evidence are ready for the user.

### Validation

```bash
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src tests
.venv/bin/python -m pytest
```

Also verify the installed console scripts with fake workers, a clean temporary workspace without
config, and representative legacy-state fixtures. Confirm process tests leave no live workers.

### Completion criteria

All quality gates and required acceptance scenarios pass. Every review finding is addressed or
explicitly documented within the agreed scope. The implementation and evidence are ready for
the user's final review; final acceptance remains the user's decision.
