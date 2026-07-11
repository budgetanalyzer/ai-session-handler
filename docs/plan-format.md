# AI Session Handler Plan Format

AI Session Handler runs executable Markdown plans. A design document can inform a plan, but it is
not itself executable until it uses explicit numbered phase headings.

## Creating A Plan

Create the plan as a Markdown file in the target repository, commonly under `docs/plans/`. Use
the following template, replace every `TODO`, and repeat the phase block as needed:

```markdown
# TODO Plan Title

TODO: Summarize the intended outcome and relevant context.

## Phase 1: TODO Phase Title

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

Each phase should be fine-grained enough for one fresh agent session. State concrete outcomes,
boundaries, validation commands or checks, and the conditions that make the phase complete.

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

## Other Headings

Headings such as `Stage`, `Workstream`, and `Issue` are planning or design headings, not execution
boundaries. The parser only recognizes headings that match `Phase N: Title`.

Convert a design document into an executable plan by choosing the concrete execution boundaries and
writing explicit `## Phase N: Title` headings. Do not rely on numbered lists or issue-local stage
headings to imply phases.
