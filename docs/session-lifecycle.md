# Session Lifecycle and Acceptance

AI Session Handler is a process runner and durable handoff mechanism. It does not own a provider's
conversation store, tool policy, semantic review, or the edits made by a worker. Those boundaries
matter when interpreting a completed phase or recovering an interrupted attempt.

## Guarantees and boundaries

| Concern | What the handler does | What it does not establish |
| --- | --- | --- |
| Worker lifecycle | Launches each selected phase in a new child process and POSIX session/process group. | A new provider conversation. A user-supplied command can resume provider-owned conversation state. |
| Execution workspace | Sets the child process working directory to the phase's validated `Workspace` and instructs the worker to operate only there. | Filesystem confinement. The handler is not a sandbox, and the command retains whatever filesystem access its OS identity and provider configuration allow. |
| Phase result | Requires one well-framed terminal result, records the worker's assertion, and advances a completed phase only for `phase-complete`. | Independent verification, correctness, or user approval. |
| Persistence | Flushes an outcome record before atomically replacing state with a reference to it. Unreferenced outcomes are never adopted as success. | Exactly-once worker execution or atomic workspace edits. A crash can leave partial edits or an unresolved process. |
| Execution ownership | Uses advisory locks to prevent cooperating handler invocations from overlapping a plan workspace or selected execution workspace. | Exclusion of editors, unrelated commands, or deliberately detached processes. |

The exact state transitions and interruption procedure are documented in
[State layout and recovery](state-and-recovery.md). Terminal result framing and handoff content are
documented in [Worker result protocol](worker-protocol.md).

## Fresh phases, continuation, compaction, and delegation

Fresh phases are useful durable checkpoints, not a claim that all work should use isolated model
contexts. Provider-native continuation, context compaction, and delegation can complement the
handler:

- A continuation can preserve detailed short-term reasoning inside one provider conversation.
- Compaction can summarize earlier turns when a provider conversation grows long.
- Native subagents can move independent exploration, tests, or review off the main thread and
  return distilled results.
- A later handler phase can still start a new process from the plan preamble, phase body, latest
  summary, and committed outcome index.

These provider features are not scheduled or recorded separately by the handler. If a worker uses
them, the phase still has one runner-owned attempt, transcript, terminal result, and acceptance
boundary. The command template determines whether a process starts or resumes a provider
conversation and which provider features are available.

Official OpenAI documentation describes [Codex subagent workflows](https://learn.chatgpt.com/docs/agent-configuration/subagents)
and the Codex CLI [`/compact` command](https://learn.chatgpt.com/docs/developer-commands?surface=cli#keep-transcripts-lean-with-compact).
Availability and behavior can change with the Codex release and configuration; consult those
sources rather than treating this repository as the owner of Codex behavior.

## Codex wrapper and external launcher

The optional `ai-session-handler-codex-high` entrypoint is a provider wrapper shipped by this
repository. It sets high reasoning effort, optionally forwards `--model` through `CODEX_MODEL`,
invokes `codex-lean exec`, filters marker-like diagnostics, and normalizes the authoritative final
message into the handler's result protocol.

`codex-lean` is a separate container-installed executable at `/usr/local/bin/codex-lean`; this
repository does not install, own, or update it. The wrapper fails if that executable is unavailable.
The installed launcher selects its own approvals, sandbox, tool set, history, feature, and model
defaults. The Phase 11 inspection found that this container's launcher disables web search, native
multi-agent features, and several other integrations while selecting no-approval,
danger-full-access execution. Those are launcher configuration choices, not consequences of using
fresh handler phases and not guarantees made by the provider-agnostic runner.

Before using this wrapper in a different container, inspect the actual `codex-lean` executable and
decide whether its permissions and features are appropriate. Changing that launcher, enabling
subagents, or selecting different permissions belongs to the external container configuration.
Omitting the wrapper's `--model` leaves model selection to an existing `CODEX_MODEL` value or the
external Codex configuration; the handler does not silently choose a model.

## Manual final review

`runner-complete` means every phase in the accepted plan snapshot reported `phase-complete`. The
intended acceptance workflow is manual:

1. Run `status` and inspect the accepted plan, changed source and documentation, committed outcome
   records, and relevant transcripts.
2. Run the repository's final validation commands independently of worker claims.
3. Review partial-work and interruption risks, including any explicit limitations in handoff
   summaries.
4. Decide whether the implementation is accepted or whether another ordinary development change
   is required.

The handler has no approval command, persisted user-acceptance field, or automated final reviewer.
Do not edit runner-owned state or outcomes to record approval.

## Optional comparative evaluation

To evaluate workflow choices, select several representative tasks with objective acceptance
checks. Hold the provider model, reasoning setting, available tools, permissions, repository
snapshot, and human acceptance criteria constant. Run separate trials using:

1. fresh handler phases at coherent checkpoints;
2. one continued provider conversation, using compaction when needed; and
3. one provider conversation using native delegation for independent work.

For each trial, record accepted correctness, number and type of human interventions, repeated
repository discovery, elapsed time, and reported token/cache usage. Repeat tasks or rotate their
order to reduce task and warm-cache bias. Treat the results as local evidence, not a universal
ranking. Running this experiment is optional future work and is not part of repository acceptance.
