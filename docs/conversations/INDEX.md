# Conversation Index

Quick reference for architecture and codebase walkthrough conversations in this repository.

---

## 001 - AI Session Handler Architecture Walkthrough

**Core insight:** AI Session Handler is a small dependency-light Python CLI, not a service. Its execution path is intentionally linear: parse CLI, parse a markdown plan, read durable JSON state, select one phase, render a worker prompt, spawn one provider-neutral subprocess, capture output, require exactly one terminal marker, update state, and exit.

**Key topics:** Python packaging, editable install, CLI entrypoints, workspace inference, generated `.ai-session-handler` files, markdown phase parsing, immutable dataclass state model, plan hash protection, worker prompt contract, subprocess spawning, stdout/stderr streaming, timeout and stop-regex handling, terminal markers, transcripts, provider-agnostic core, Codex wrapper, pytest fake-agent tests

*Signed: conversations-codex*

