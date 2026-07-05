"""Transcript file handling for agent process runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TranscriptHeader:
    """Metadata written at the top of each transcript."""

    run_id: str
    phase_id: str
    phase_title: str
    plan_path: Path
    state_path: Path
    started_at: str
    agent_cmd: str


def transcript_path(generated_dir: Path, run_id: str) -> Path:
    """Return the transcript path for a run id."""
    return generated_dir / "transcripts" / f"{run_id}.txt"


def render_transcript_header(header: TranscriptHeader) -> str:
    """Render the deterministic transcript header."""
    return (
        f"run_id: {header.run_id}\n"
        f"phase: {header.phase_id} {header.phase_title}\n"
        f"plan: {header.plan_path}\n"
        f"state: {header.state_path}\n"
        f"started_at: {header.started_at}\n"
        f"agent_cmd: {header.agent_cmd}\n"
        "---\n"
    )
