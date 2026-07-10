# AI Session Handler Plan Format

AI Session Handler runs executable Markdown plans. A design document can inform a plan, but it is
not itself executable until it uses explicit numbered phase headings.

## Creating A Plan

Create a scaffold with the globally installed command:

```bash
ai-session-handler create-plan --plan docs/plans/example.md
```

The command creates missing parent directories and refuses to overwrite an existing file. It does
not create `.ai-session-handler/`, initialize config, or write runner state.

The scaffold starts with this handler-owned marker:

```markdown
<!-- ai-session-handler-template: incomplete -->
```

`run` and `status` reject a plan while that marker remains. Replace every placeholder, then remove
the marker before treating the file as executable.

## Executable Phases

Every executable unit must use this heading shape:

```markdown
## Phase 1: Title
```

The parser accepts any Markdown heading level, but the heading text must match:

```text
Phase N: Title
```

`N` must be a positive integer. Phase numbers must be unique and strictly increasing. Phase ids are
derived from those numbers, so `Phase 1` becomes `phase-1`.

The body of a phase is preserved exactly from the line after its heading through the line before the
next phase heading. Use sections such as `Goal`, `Scope`, `Validation`, and `Completion criteria`
inside the phase body.

## Non-Executable Planning Headings

Headings such as `Stage`, `Workstream`, and `Issue` are planning or design headings, not execution
boundaries. The parser may report their line numbers in an error to help authors repair a document,
but it never interprets them as phases.

Convert a design document into an executable plan by choosing the concrete execution boundaries and
writing explicit `## Phase N: Title` headings. Do not rely on numbered lists or issue-local stage
headings to imply phases.
