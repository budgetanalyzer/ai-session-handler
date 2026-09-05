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
        └── transcripts/
            └── <attempt-id>.txt
```

`<plan-key>` is the SHA-256 digest of the canonical workspace-relative plan path encoded as UTF-8.
The key separates path identity from the content hash stored in `state.json`: identical plans at
different paths have independent history, while an edit at one path is detected as a content-hash
change. Path aliases that resolve to the same canonical file use the same key.

Attempt ids retain a UTC timestamp and phase id for operators and include a UUID for uniqueness.
Prompt and transcript files are created exclusively. If an artifact with the selected attempt id
already exists, the runner stops instead of truncating or replacing it.

## Schema 2 Attempt Lifecycle

Schema 2 distinguishes work that has never started from an attempt whose terminal outcome was not
durably recorded. Before creating attempt artifacts or launching a worker, the runner atomically
records `active_attempt` with status `prepared`. That record contains the attempt and phase ids,
the accepted plan path and SHA-256 snapshot, execution workspace, start time, and absolute prompt
and transcript paths. Immediately after launch it replaces that record with status `running` and,
when Linux process metadata is readable, records the PID, process-group id, boot id, and process
start time. The boot id and start time prevent a later retry from treating an unrelated process
that reused the numeric PID as the old worker.

A terminal transition atomically clears `active_attempt` and records `last_run`. Completion also
adds the phase id to `completed_phase_ids` and clears `current_phase`. Blocked, clarification, agent
failure, marker failure, timeout, stop-regex, launch failure, interruption, and execution IO
outcomes retain `current_phase` and create a typed `stop`. Clarification text remains in
`stop.clarification_request`; other diagnostic text remains in `stop.message`. A launch failure
after the prepared record exists and any attempt IO failure return exit code 4. A catchable SIGINT
or SIGTERM records the interrupted outcome before the handler exits in response to that signal.
Invalid plans, configuration, command templates, workspaces, or persisted state rejected before
preparation return exit code 5.

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
cannot replace `state.json`, the last durable prepared/running attempt is retained and the
invocation returns exit code 4. If interruption recording fails, the signal-driven exit continues
and the active attempt remains unresolved. Both cases deliberately lose a known-but-not-durable
result rather than erasing the evidence that execution occurred.

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
manual retry; the schema 2 active attempt makes that inspection and explicit retry mandatory.

## Manual Schema 1 to Schema 2 Transition

There is no automatic schema migration. Schema 1 could contain `current_phase` after a worker had
started but did not retain enough attempt or process identity to determine whether that work was
untouched, partially applied, or complete. Changing only `schema_version` would make an unsafe
state appear valid.

To convert an existing keyed `state.json`:

1. Stop all handler and worker processes for the plan and back up its complete keyed history
   directory, including state, prompts, and transcripts.
2. Verify the stored `plan.path` is the intended canonical absolute plan path and its stored
   `plan.sha256` matches the accepted snapshot. Preserve the plan record, completed phase ids, and
   all timestamps exactly.
3. Add `"active_attempt": null`. If schema 1 has a `current_phase` but no `stop`, do not clear it:
   add a stop with `reason` set to `interrupted`, the same `phase_id`, a message explaining that the
   legacy attempt outcome is unknown, and a null `clarification_request`. Inspect the workspace and
   artifacts before eventually using `--retry-stopped`.
4. Preserve an existing stop exactly, including its message or clarification request. Confirm its
   phase id matches `current_phase` and is not listed as completed.
5. Preserve `last_run`, but change its `status` only if necessary to one of the documented typed
   values. Add `execution_workspace` from the corresponding phase/transcript header and add the
   absolute prompt path under this plan's `prompts/` directory. Convert `transcript_path` to its
   absolute path. Both artifact filenames must be `<last_run.run_id>.txt`; do not invent or move a
   reference without checking the artifact.
6. Set `schema_version` to `2`, write valid JSON to a separate file, then atomically replace
   `state.json`. Run `status --plan PATH` and resolve every named key error before attempting a
   retry.

If a schema 1 field or artifact cannot be assigned confidently, keep the backup and make an
explicit archive/fresh-start decision. Do not infer completion or fabricate an active process
identity. A schema 1 `current_phase` without a stop must always be carried as an interrupted stop,
even when no matching artifact can be found.

## Legacy Stem-Based Layout

Earlier versions stored plan state at `.ai-session-handler/<plan-stem>.json` and mixed all prompts
and transcripts in workspace-level `prompts/` and `transcripts/` directories. `run` and `status`
detect that state before creating keyed history and return an input error with the expected new
state path. They do not modify the legacy file or artifacts.

A plan named `config.md` previously mapped its state to `.ai-session-handler/config.json`, which is
also the current shared configuration path. A JSON object containing the legacy state
`schema_version` is diagnosed as legacy state, not accepted as an empty/default configuration. A
normal handler configuration at that path remains valid.

There is intentionally no automatic migration because equal stems may have combined artifacts
from different plans. To carry legacy history forward:

1. Stop all handler and worker processes for the workspace.
2. Back up the entire `.ai-session-handler/` directory before moving or editing anything.
3. Read the legacy state's `plan.path` and verify it is the canonical absolute path of the intended
   plan. Independently compare its stored `plan.sha256` with the intended plan bytes. Resolve any
   mismatch before assigning history.
4. Use the destination printed by the transition error, or obtain it with the repository-local
   environment:

   ```bash
   .venv/bin/python -c 'from pathlib import Path; from ai_session_handler.config import default_state_path; workspace = Path(".").resolve(); plan = Path("docs/plans/PLAN.md").resolve(); print(default_state_path(workspace, plan))'
   ```

5. Create the destination plan directory and move the verified legacy state to its `state.json`.
6. Inspect legacy prompt contents and transcript headers for their canonical `plan_path` or `plan`
   field. Move only artifacts proven to belong to this plan into the destination `prompts/` and
   `transcripts/` directories. Never overwrite a name collision; retain unassigned or ambiguous
   artifacts with the backup for manual review.
7. If the latest transcript moved, update `last_run.transcript_path` in the relocated state to its
   new absolute path while preserving the existing schema and other fields.
8. For the `config.md` collision, create a normal shared `config.json` after moving the state away.
9. Run `status --plan PATH` and inspect the reported state path, accepted plan identity, next or
   stopped phase, and latest transcript before retrying work.

If the legacy `schema_version` is unsupported, archive it and make an explicit fresh-start or
manual-conversion decision rather than changing the version number alone.

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
