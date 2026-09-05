# AI Session Handler Plan Format

AI Session Handler runs executable Markdown plans. A design document can inform a plan, but it is
not executable until it uses the explicit hierarchy `Plan -> Phase -> Execution steps`.

## Creating A Plan

Create the plan as a Markdown file in the plan repository, commonly under `docs/plans/`. Before
filling in detailed steps, sketch the independently verifiable implementation checkpoints and give
each checkpoint its own phase. Reusing the same workspace and reopening the same files in later
phases is normal. Use the following template, replace every `TODO`, and repeat the phase block for
each checkpoint:

```markdown
# TODO Plan Title

TODO: Summarize the intended outcome and relevant context.

## Phase 1: TODO Phase Title

### Workspace

TODO: One relative repository path, such as `.` or `../transaction-service`.

### Goal

TODO

### Scope

TODO

### Non-goals

TODO

### Required context

TODO

### Execution steps

1. TODO
2. TODO

### Implementation notes

TODO

### Validation

TODO

### Completion criteria

TODO
```

## Plans, Phases, And Execution Steps

A plan contains one or more phases. A phase is one fresh agent session and contains one or more
execution steps. Execution steps describe the concrete work within that session; they are not
separate runner checkpoints.

Treat a phase as a conservative fresh-session allocation, not as a feature, issue, commit, or the
largest body of work that could theoretically fit in one context window. The goal is to hand work
to a fresh agent before long-session reasoning degrades or compaction becomes likely.

Plan an ordinary phase around 25–35% of the available context window. Treat roughly 40% as a
planning warning threshold, not a utilization target. The unused capacity is deliberate headroom
for repository discovery, unexpected debugging, and validation. Context use is a planning
calibration and is not measured by the runner.

As a secondary calibration, an ordinary phase should represent roughly ten minutes of focused
agent work under expected conditions. This is not a timeout or runtime guarantee, but a phase that
plausibly requires 20–30 minutes before reaching its first durable checkpoint is too large and
should be split.

Default to a fresh phase at each independently verifiable implementation checkpoint. Start a new
phase when at least one of these applies:

- the execution workspace or repository changes;
- an independent decision or human review gate must occur between the work;
- the transition is risky enough to require an isolated validation boundary;
- the current milestone can leave a coherent, focused-test-passing state for the next milestone;
- the phase contains more than about five substantial execution steps;
- the phase spans three or more major concerns such as API contracts, controllers or adapters,
  service/domain behavior, persistence, integration coverage, and documentation; or
- the combined working set could approach 40% of a context window under expected conditions.

Shared files, abstractions, repository instructions, or validation commands are not reasons to
merge substantial phases. A later worker can reread and modify the same files; that small warm-up
cost is the intended tradeoff for fresh reasoning. Merge adjacent phases only when both are small,
tightly coupled, and their combined work still satisfies the conservative size guidance above.

A broad change spanning three or more major concerns should normally have at least three phases,
even when every phase uses `.` and edits overlapping files. A typical vertical change might use:

1. contracts and the first coherent implementation path, with focused unit tests;
2. the remaining domain/persistence behavior, with focused service and integration tests; and
3. cross-cutting integration, documentation, cleanup, and the full validation suite.

Each phase must leave a durable, understandable worktree checkpoint. Its completion criteria and
focused validation should state what a fresh next worker can rely on. Do not postpone all tests and
coherence checks until the final phase; reserve the final full-suite phase for integration and
hardening after earlier focused checks pass.

Repository switching is a hard phase boundary. A phase must perform implementation, validation,
and other execution work in exactly one repository: its declared execution workspace. If any
execution step requires operating in another repository, put that work in a separate phase whose
workspace names that repository. Never merge work across repositories into one phase, regardless
of shared context or available context capacity.

State concrete outcomes, boundaries, execution steps, validation commands or checks, and the
conditions that make each phase complete. A worker executes exactly one selected phase and never
continues into a later phase in the same process.

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

The body of a phase is preserved exactly from the line after its heading through the line before
the next phase heading. Use the canonical sections above to make the work explicit.

The plan must be valid UTF-8. At invocation initialization, the runner reads the plan bytes once,
hashes those exact bytes, and parses the same read into an immutable snapshot. Line endings are
not normalized: CRLF bytes contribute to the accepted hash and remain CRLF in captured preamble
and phase bodies. Text before the first executable phase is the global preamble. It is retained as
plan-wide intent in the snapshot, but it is not itself an execution unit.

The supported fenced-code subset uses backtick or tilde fences of at least three matching
characters, optionally indented by up to three spaces. A closing fence must use the same character
and at least the opening length. Backtick fence info text cannot contain a backtick. Phase and
workspace headings inside a valid fence are examples or code, not executable structure. An
unclosed supported fence is a plan error reported at its opening line.

## Phase Workspace

Every phase must contain exactly one `### Workspace` section. Its content must be exactly one
non-empty relative path line. `.` selects the plan repository; a path such as
`../transaction-service` selects a sibling repository. Absolute paths are invalid.

The declared workspace is the only repository in which that phase may perform execution work. It
is not a starting directory from which the phase may switch to or modify other repositories.

Workspace paths resolve from the plan repository root, not from the plan file's directory or the
caller's current directory. The resolved path must be an existing directory with an `AGENTS.md` at
that exact root. This ensures the fresh worker's first repository instructions belong to the
repository where the phase executes.

The plan repository and execution workspace have separate roles. The plan repository owns
`.ai-session-handler/config.json`, state, generated prompts, and transcripts for the whole plan.
The selected phase workspace is only the child process working directory and the value of the
`{workspace}` command placeholder.

## Other Headings

Headings such as `Stage`, `Workstream`, and `Issue` are planning or design headings, not execution
boundaries. The parser only recognizes headings that match `Phase N: Title`.

Convert a design document into an executable plan by choosing session-sized execution boundaries,
grouping concrete steps inside them, and writing explicit `## Phase N: Title` headings. Do not rely
on numbered lists or issue-local stage headings to imply phases.

The accepted plan snapshot remains fixed for one invocation. The runner checks the source bytes
again before each later worker launch and before reporting that the whole plan is complete. If a
worker or another process edits the source while a phase runs, that worker's recorded outcome is
retained as an assertion about the original snapshot, but no later worker launches. Inspect the
edit and use `--accept-plan-change` in a new invocation if the new plan should be accepted.
