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
