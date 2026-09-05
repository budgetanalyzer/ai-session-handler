# Worker Result Protocol

AI Session Handler invokes a worker command with the selected phase prompt on standard input. The
command may write ordinary progress and diagnostics to either standard output or standard error.
It reports its final assertion with one textual result block using one of these tags:

```text
<phase-complete>summary</phase-complete>
<phase-blocked>reason</phase-blocked>
<phase-needs-clarification>specific question for user</phase-needs-clarification>
```

The result text may span lines, but it must contain non-whitespace content. The opening tag must
start at a line boundary, with no preceding indentation or prose on that line. The closing tag must
be the final non-whitespace content of the stream that emits it. A worker may emit the result on
stdout or stderr, but not both.

Recognized result tags are reserved protocol text. A worker must not put them in progress output,
diagnostics, quotations, fixtures, or fenced examples. Extra blocks, nested or mismatched tags,
empty blocks, unclosed or malformed recognized tags, an inline opening tag, trailing non-whitespace
content, and recognized tags on both streams all make the result ambiguous and fail the attempt.
The runner validates stdout and stderr independently, so scheduling between the two pipes cannot
join fragments into a result or make unrelated cross-stream ordering significant.

The runner accepts a well-framed result only after the worker exits successfully and without a
timeout or controlled stop. A nonzero exit, timeout, or stop-regex result takes precedence over a
completion block. Terminal blocks remain in the durable transcript but are hidden from live output;
the runner prints the recorded outcome after updating state.

The runner consumes both streams as bounded, incrementally decoded UTF-8 chunks. Marker framing is
tracked incrementally per stream, so ordinary diagnostics do not also remain in memory solely for
result parsing; the complete untruncated output remains in the transcript. Queue draining is
budgeted so sustained worker output cannot indefinitely postpone timeout and stop checks, and all
normally produced chunks are drained to the transcript before final marker validation.

Stop regexes are the deliberate exception to bounded output retention. Their documented semantics
search the full combined stdout/stderr history, including output received during the final drain.
When configured, that history remains in memory and is searched again after new output. Very large
histories and arbitrary Python regular expressions may therefore have significant memory or CPU
cost, and regex execution itself has no time bound.

These outcomes are worker assertions. `phase-complete` advances execution history, but it is not
independent verification or user approval. The user remains responsible for reviewing the changes
and validation evidence and for final semantic acceptance.

## Fresh-Session Context and Handoffs

Every worker prompt contains clearly delimited copies of the accepted plan's global preamble and
the selected phase body. Workers must read both, follow referenced context, and perform execution
work only for the selected phase in its declared execution workspace. The prompt also carries the
latest relevant terminal summary and a compact phase/status/path index of earlier runner-committed
outcomes. Workers should open indexed JSON records when earlier decisions or verification evidence
is relevant; complete transcripts are retained separately rather than copied into each session.

Every terminal result body remains plain text, with no runner-parsed internal heading grammar. It
should concisely identify changed artifacts, validation commands and results, decisions, remaining
limitations, and relevant handoff references. When a decision must remain available beyond the
generated execution history, the worker records it in ordinary repository documentation as part of
the selected phase.

State and outcome files are runner-owned. Workers may read committed outcomes referenced by the
prompt, but must not create, edit, replace, or delete state or outcome records. The runner writes an
outcome record before committing its reference to state; an unreferenced record left by an
interruption is evidence only and never establishes completion.

## Provider Wrapper Obligations

Core execution remains provider-agnostic. A wrapper that translates provider-specific output must:

- preserve ordinary useful stdout and stderr diagnostics without emitting recognized result tags;
- handle tags split across reads and multiline blocks without leaking live result blocks;
- sanitize recognized tag text in diagnostics so it cannot be parsed as a worker result;
- derive the terminal result only from the provider's authoritative final response;
- validate that response with the framing rules above and emit at most that one normalized result;
- preserve the provider process exit code and keep its child within the handler-owned process group.

The bundled `ai-session-handler-codex-high` wrapper streams sanitized live output and reads the
Codex final-message file. It re-emits a result only when that file contains one valid final block.
If the final message is invalid, it emits the message as sanitized diagnostics, leaving the core
runner to record marker failure.
