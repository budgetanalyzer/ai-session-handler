# State Layout and Recovery

AI Session Handler keeps configuration shared at the plan-workspace level and gives every plan
path a separate history directory:

```text
.ai-session-handler/
├── config.json
└── plans/
    └── <plan-key>/
        ├── state.json
        ├── prompts/
        │   └── <attempt-id>.txt
        ├── outcomes/
        │   └── <attempt-id>.json
        └── transcripts/
            └── <attempt-id>.txt
```

`<plan-key>` is the SHA-256 digest of the canonical workspace-relative plan path encoded as UTF-8.
The key separates path identity from the content hash stored in `state.json`: identical plans at
different paths have independent history, while an edit at one path is detected as a content-hash
change. Path aliases that resolve to the same canonical file use the same key.

Attempt ids retain a UTC timestamp and phase id for operators and include a UUID for uniqueness.
Prompt, transcript, and outcome files are created exclusively. If an artifact with the selected
attempt id already exists, the runner stops instead of truncating or replacing it.

## Attempt Lifecycle

Current state distinguishes work that has never started from an attempt whose terminal outcome was
not durably recorded. Before creating attempt artifacts or launching a worker, the runner
atomically records `active_attempt` with status `prepared`. That record contains the attempt and
phase ids, the accepted plan path and SHA-256 snapshot, execution workspace, start time, and
absolute prompt, transcript, and planned outcome paths. Immediately after launch it replaces that
record with status `running` and, when Linux process metadata is readable, records the PID,
process-group id, boot id, and process start time. The boot id and start time prevent a later retry
from treating an unrelated process that reused the numeric PID as the old worker.

A terminal transition first writes and flushes a runner-owned JSON outcome record, then atomically
clears `active_attempt`, appends a typed reference to `committed_outcomes`, and records `last_run`.
The unversioned outcome object contains attempt, accepted plan snapshot, and phase identity; terminal
status and plain-text summary; start and finish timestamps; execution workspace; and prompt and
transcript paths. Completion also adds the phase id to `completed_phase_ids` and clears
`current_phase`. Blocked, clarification, agent failure, missing, multiple, or invalid marker
failure, timeout, stop-regex, launch failure, interruption, and execution IO outcomes retain
`current_phase` and create a typed `stop`. Clarification text remains in
`stop.clarification_request`; other diagnostic text remains in `stop.message`. A launch failure
after the prepared record exists and any attempt IO failure return exit code 4. A catchable SIGINT
or SIGTERM records the interrupted outcome before the handler exits in response to that signal.
Invalid plans, configuration, command templates, workspaces, or persisted state rejected before
preparation return exit code 5.

Only outcome paths referenced by `committed_outcomes` are authoritative history. If the handler
writes an outcome but crashes or cannot replace state, that record remains useful execution
evidence but is uncommitted and is never adopted later as proof of success. The durable
`active_attempt` still requires inspection and explicit retry. Recovery resolves that attempt as
an unknown interruption; a later successful attempt receives its own id and outcome record.

Fresh worker prompts include the accepted plan's exact global preamble, the exact selected phase
body, the latest relevant terminal summary, and a compact index of earlier committed outcome paths.
The index labels phase and status so failed attempts are not presented as completed prerequisites.
Workers read relevant indexed records when earlier decisions or validation evidence matter; full
transcripts remain available but are not replayed into every prompt. Durable design decisions that
later phases depend on belong in ordinary repository documentation, not only in generated history.

Terminal results are validated independently on stdout and stderr using the framing contract in
[Worker result protocol](worker-protocol.md). The runner records `invalid-marker` for recognized but
badly framed tag text and `multiple-markers` for duplicate results, including results emitted on
both streams. These protocol failures do not advance completed phase ids.

If the handler disappears while an attempt is prepared or running, the durable `active_attempt`
is intentionally left unresolved. A normal `run` refuses to launch more work. `status` reports the
attempt, workspace, prompt, transcript, and recorded PID without changing state, including when
the current plan content has a hash mismatch. The operator must inspect those artifacts, inspect
partial workspace changes, and stop any surviving worker before using `--retry-stopped`.

On retry, a process is considered positively identified only when its current PID, process group,
boot id, and start time all match the durable identity and it is not a zombie. The runner refuses
to overlap such a worker. It never signals a process during recovery based only on a stored PID.
An absent or mismatched identity permits the explicit retry because the operator has asserted that
inspection and cleanup are complete. The old attempt is first resolved as `interrupted` with an
unknown result, then the new prepared attempt and that resolution are written in one replacement.

There is an unavoidable launch-to-identity-record window: the process can start after `prepared`
is durable and the handler can die before `running` and its process identity are written. Such a
record has unknown process identity. Treat it as potentially live and perform manual process
inspection; the runner will neither guess liveness nor kill a numeric PID for it.

State replacement can itself fail. If an ordinary terminal result or handled launch/IO failure
cannot replace `state.json`, the last durable prepared/running attempt is retained and any already
written outcome stays uncommitted. The invocation returns exit code 4. If interruption recording
fails, the signal-driven exit continues and the active attempt remains unresolved. Both cases
deliberately lose a known-but-not-durable result rather than erasing the evidence that execution
occurred.

## Exclusive Execution Ownership

Each `run` invocation takes a nonblocking advisory lock on the existing canonical plan-workspace
directory before it reads runner state. The lock is held for the entire invocation, so separate
plans owned by one plan workspace cannot execute concurrently. Before changing state for a selected
phase, the runner also locks that phase's canonical execution-workspace directory. It holds this
lock through worker cleanup and durable result recording. When the plan and execution workspaces
are the same directory, the already-held plan lock is reused.

If either directory is already owned by another cooperating handler invocation, the contender
returns immediately with exit code 5. It does not wait, launch a worker, clear a stopped phase, or
change attempt state. The locks use `fcntl.flock` on directory descriptors and create no lock file
or other generated artifact in an execution workspace. Lock descriptors are not inherited by
worker commands. Canonical paths and directory identity make path aliases refer to the same lock.

These are advisory Linux-container locks: they coordinate handler invocations but do not exclude
editors, arbitrary tools, or programs that do not take the same locks. The OS releases a lock when
the handler closes its descriptor or exits, including after abrupt process termination. That
release establishes only that no live handler owns the lock; it does not prove that an interrupted
worker made no partial edits or that its last outcome reached durable state. Inspect the workspace,
processes, state, prompt, and transcript before retrying work after a crash.

## Worker Process Ownership

The Linux-container runner starts every worker in a new POSIX session and process group. The
worker command, its provider wrapper, and ordinary descendants inherit that group. A timeout,
stop-regex match, catchable handler interruption, or execution/streaming exception causes the
runner to signal the whole group with SIGTERM, allow a short grace period, and then send SIGKILL
if members remain. Cleanup is based on the group identity retained at launch, so it still runs if
the original group leader exits before a resistant descendant. The runner never signals its own
process group.

Pipes, transcript handles, and stream threads are closed or joined after process cleanup. Attempt
artifacts are opened and their transcript header is written before launch where possible, so an
initial artifact IO failure does not start a worker. The bundled Codex wrapper does not create a
nested session or process group: its `codex-lean` child remains reachable by the outer handler's
group cleanup, and direct wrapper exceptions also terminate that immediate child.

This is lifecycle management, not an OS sandbox. It cannot guarantee cleanup if the handler is
killed with SIGKILL, the container or kernel stops abruptly, or a descendant deliberately
daemonizes into another session/process group. It also cannot make partial workspace edits
exactly-once. After an abrupt interruption, inspect the workspace and process table before any
manual retry; the active attempt makes that inspection and explicit retry mandatory.

## Release-Scoped Generated Data

Plans and their generated state are release-scoped. Finish a partially executed plan with the same
AI Session Handler release that created its state. After upgrading the handler, begin new work with
fresh generated state instead of continuing an existing execution.

The runner reads and writes only its current state and artifact formats. It provides no migration,
compatibility reader, or supported mixed-release workflow. A state object with missing,
unexpected, or malformed keys is reported as invalid current data; the runner does not classify or
translate it. Generated files outside the current keyed plan directory are not searched for,
interpreted, moved, rewritten, or deleted.

Existing generated files remain user-owned execution evidence. They may be retained or archived
manually, but they are not inputs to the current release unless they were created by that release
at the current keyed paths.

## Clarification and Durable Context

When a worker requests clarification, record the answer in durable plan or repository context. If
the answer changes the plan bytes, inspect the change and run the stopped phase with both
`--retry-stopped` and `--accept-plan-change`; the runner checks that every completed phase id still
exists before accepting the new snapshot. Use a plan preamble edit for execution-wide intent and
ordinary repository documentation for design context that should outlive generated runner history.
Do not edit `state.json` or outcome records to supply an answer.

## Renamed or Relocated Plans

A rename produces a different plan key. Choose explicitly between fresh history and carried
history:

- For a fresh start, retain or archive the old keyed directory and run the plan at its new path.
- To carry history, first back up the generated directory, verify that the old state and artifacts
  belong to the renamed plan, move the entire keyed directory to the new key, change the stored
  `plan.path` to the new canonical path, and update artifact paths recorded in state. If the content
  also changed, inspect it and then use `--accept-plan-change` so completed phase ids are checked.

The runner validates the stored canonical path even when content hashes match and even when
`--accept-plan-change` is supplied. It will not infer that two paths represent the same plan.
