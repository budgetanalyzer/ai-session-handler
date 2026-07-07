"""Codex high-reasoning exec wrapper with terminal marker filtering."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

from ai_session_handler.markers import MarkerKind

_MARKER_TAG_PATTERN: Final[str] = "|".join(re.escape(kind.value) for kind in MarkerKind)
_MARKER_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"<(?P<tag>{_MARKER_TAG_PATTERN})>(?P<text>.*?)</(?P=tag)>",
    re.DOTALL,
)
_MARKER_OPEN_PATTERN: Final[re.Pattern[str]] = re.compile(rf"<(?P<tag>{_MARKER_TAG_PATTERN})>")


@dataclass(slots=True)
class _TerminalMarkerFilter:
    open_tag: str | None = None

    def filter(self, text: str) -> str:
        visible_parts: list[str] = []
        remaining = text
        while remaining:
            if self.open_tag is not None:
                close_tag = f"</{self.open_tag}>"
                close_index = remaining.find(close_tag)
                if close_index == -1:
                    return "".join(visible_parts)
                remaining = remaining[close_index + len(close_tag) :]
                self.open_tag = None
                continue

            match = _MARKER_OPEN_PATTERN.search(remaining)
            if match is None:
                visible_parts.append(remaining)
                break

            visible_parts.append(remaining[: match.start()])
            self.open_tag = match.group("tag")
            remaining = remaining[match.end() :]

        return "".join(visible_parts)


def _sanitize_markers(text: str) -> str:
    sanitized = text
    for marker_kind in MarkerKind:
        tag = marker_kind.value
        sanitized = sanitized.replace(f"<{tag}>", f"[{tag}]")
        sanitized = sanitized.replace(f"</{tag}>", f"[/{tag}]")
    return sanitized


def _write_stdin(stream: TextIO, prompt: str) -> None:
    try:
        stream.write(prompt)
    except (BrokenPipeError, OSError, ValueError):
        return
    finally:
        with suppress(BrokenPipeError, OSError, ValueError):
            stream.close()


def _stream_filtered_output(source: TextIO, target: TextIO) -> None:
    marker_filter = _TerminalMarkerFilter()
    while True:
        chunk = source.readline()
        if chunk == "":
            break
        visible_chunk = _sanitize_markers(marker_filter.filter(chunk))
        if visible_chunk:
            target.write(visible_chunk)
            target.flush()


def main() -> int:
    """Run Codex in high-reasoning mode and emit only one terminal marker."""
    prompt = sys.stdin.read()
    env = os.environ.copy()
    env["CODEX_REASONING_EFFORT"] = "high"

    with tempfile.TemporaryDirectory(prefix="ai-session-handler-codex-") as temp_dir:
        final_message_path = Path(temp_dir) / "final-message.txt"
        process = subprocess.Popen(
            [
                "codex-lean",
                "exec",
                "--color",
                "never",
                "--output-last-message",
                str(final_message_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            encoding="utf-8",
            errors="replace",
        )

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        threads = [
            threading.Thread(target=_write_stdin, args=(process.stdin, prompt), daemon=True),
            threading.Thread(
                target=_stream_filtered_output,
                args=(process.stdout, sys.stdout),
                daemon=True,
            ),
            threading.Thread(
                target=_stream_filtered_output,
                args=(process.stderr, sys.stderr),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()

        return_code = process.wait()
        for thread in threads:
            thread.join(timeout=1)

        try:
            final_message = final_message_path.read_text(encoding="utf-8")
        except OSError:
            final_message = ""

        matches = list(_MARKER_PATTERN.finditer(final_message))
        if len(matches) == 1:
            sys.stdout.write(matches[0].group(0))
            sys.stdout.write("\n")
        elif final_message.strip():
            sys.stdout.write(_sanitize_markers(final_message))
            if not final_message.endswith("\n"):
                sys.stdout.write("\n")

        return return_code


if __name__ == "__main__":
    raise SystemExit(main())
