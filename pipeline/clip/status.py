"""Where a clip-mode source stands.

One description of a source's progress — taken in, transcribed, highlights
recorded, clips cut — shared by the command line (`run_clip status`) and
the dashboard's Clip mode view, so the two can't tell different stories.

Read-only: nothing here writes, cuts or publishes. It goes through the
same loaders the stages use, so a source whose provenance has been
tampered with is reported as broken rather than described.
"""

from __future__ import annotations

from typing import Any

from pipeline.clip import intake as intake_stage
from pipeline.clip import transcribe as transcribe_stage
from pipeline.clip.cut import staged_clips
from pipeline.clip.highlights import HighlightsError, load_highlights
from pipeline.clip.intake import IntakeError, load_source, provenance
from pipeline.clip.transcribe import TranscribeError, load_transcript

STAGE_ERRORS = (IntakeError, TranscribeError, HighlightsError)

# The stage to run next, named after its run_clip subcommand.
NEXT_TRANSCRIBE = "transcribe"
NEXT_FIND_HIGHLIGHTS = "find-highlights"
NEXT_CUT = "cut"

_HIGHLIGHT_FIELDS = (
    "rank",
    "start",
    "end",
    "duration_seconds",
    "hook_title",
    "score",
    "rationale",
)


def _span(highlight: dict[str, Any]) -> tuple[Any, Any]:
    return (highlight.get("start"), highlight.get("end"))


def describe_source(source_id: str) -> dict[str, Any]:
    """Describe one source: its provenance, how far it has got, and what's next.

    Raises IntakeError, TranscribeError or HighlightsError (STAGE_ERRORS)
    if the source or one of its recorded stages can't be trusted.
    """
    _, metadata = load_source(source_id)
    clips = staged_clips(source_id)
    description: dict[str, Any] = {
        "source_id": source_id,
        "title": metadata["title"],
        "origin": metadata["source_url"] or metadata["original_filename"],
        "duration_seconds": metadata.get("duration_seconds"),
        "fetched_at": metadata.get("fetched_at"),
        "rights_basis": metadata["rights_basis"],
        "source": provenance(metadata),
        "transcribed": False,
        "language": None,
        "segments": None,
        "highlights_recorded": False,
        "highlights": [],
        "clips_cut": 0,
        "other_staged_clips": len(clips),
        "next": NEXT_TRANSCRIBE,
    }

    work_dir = transcribe_stage.WORK_DIR / f"clip-{source_id}"
    if not (work_dir / "transcript.json").is_file():
        return description
    _, transcript = load_transcript(source_id)
    description.update(
        transcribed=True,
        language=transcript.get("language"),
        segments=len(transcript["segments"]),
        next=NEXT_FIND_HIGHLIGHTS,
    )

    if not (work_dir / "highlights.json").is_file():
        return description
    _, record = load_highlights(source_id)
    staged_by_span = {_span(manifest["highlight"]): (job_id, manifest) for job_id, manifest in clips}
    highlights = []
    for highlight in sorted(record["highlights"], key=lambda item: item["rank"]):
        job_id, manifest = staged_by_span.get(_span(highlight), (None, {}))
        highlights.append(
            {
                **{field: highlight[field] for field in _HIGHLIGHT_FIELDS},
                "job_id": job_id,
                "format_compliance": manifest.get("format_compliance"),
            }
        )
    current_spans = {_span(highlight) for highlight in highlights}
    clips_cut = sum(1 for highlight in highlights if highlight["job_id"] is not None)
    description.update(
        highlights_recorded=True,
        highlights=highlights,
        clips_cut=clips_cut,
        # Staged from this source, but from picks that have since been replaced.
        other_staged_clips=sum(
            1 for _, manifest in clips if _span(manifest["highlight"]) not in current_spans
        ),
        next=NEXT_CUT if clips_cut < len(highlights) else None,
    )
    return description


def list_sources() -> list[dict[str, Any]]:
    """Describe every clip-mode source, newest first.

    A source that can't be described is still listed, as
    {"source_id", "error"}: hiding it would hide the problem.
    """
    root = intake_stage.RAW_CLIP_DIR
    if not root.is_dir():
        return []
    sources: list[dict[str, Any]] = []
    for path in sorted(root.iterdir(), key=lambda entry: entry.name, reverse=True):
        if not path.is_dir() or not intake_stage._SOURCE_ID.match(path.name):
            continue
        try:
            sources.append(describe_source(path.name))
        # Wider than STAGE_ERRORS on purpose: a file that isn't text, can't
        # be opened, or has an unexpected shape must cost one row, not the list.
        except (ValueError, TypeError, KeyError, OSError) as exc:
            sources.append({"source_id": path.name, "error": str(exc) or type(exc).__name__})
    return sources
