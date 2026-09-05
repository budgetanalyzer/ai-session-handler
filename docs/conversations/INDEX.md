# Historical Conversation Archive

These conversations are dated snapshots retained for context. They are not active implementation
contracts. Use the [README](../../README.md), [plan format](../plan-format.md),
[session lifecycle](../session-lifecycle.md), [state and recovery](../state-and-recovery.md), and
[worker protocol](../worker-protocol.md) for current behavior.

---

## [001 - AI Session Handler Architecture Walkthrough](001-ai-session-handler-architecture-walkthrough.md) (historical)

**Snapshot:** A step-by-step explanation of the initial dependency-light Python CLI, including the
then-current layout, defaults, runtime flow, wrapper, and test suite. Several concrete details were
superseded by later reliability work; follow the active documents linked above.

**Key topics:** Python packaging, editable install, CLI entrypoints, workspace inference, generated `.ai-session-handler` files, markdown phase parsing, immutable dataclass state model, plan hash protection, worker prompt contract, subprocess spawning, stdout/stderr streaming, timeout and stop-regex handling, terminal markers, transcripts, provider-agnostic core, Codex wrapper, pytest fake-agent tests

*Signed: conversations-codex*
