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

These outcomes are worker assertions. `phase-complete` advances execution history, but it is not
independent verification or user approval. The user remains responsible for reviewing the changes
and validation evidence and for final semantic acceptance.

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
