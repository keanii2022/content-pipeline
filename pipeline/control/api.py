"""JSON-in/JSON-out adapter over the pipeline's deterministic stage
functions, invoked by review-app's control-panel UI via subprocess.

This module contains no pipeline logic of its own — it only translates
between a JSON action call and the exact same functions run_single.py's
CLI wraps, so there is exactly one implementation of each stage regardless
of whether it's invoked from the CLI or the control panel. Discovery,
script drafting, and the human script-approval checkpoint are NOT exposed
here — those still require a live Claude Code session (see PLAN.md Step 13).

Usage: python -m pipeline.control.api <action> <json-args>
Always prints one JSON object to stdout: {"ok": true, "result": {...}} on
success, {"ok": false, "error": "..."} on a known pipeline error (exit 1)
or a malformed call (exit 2). An unexpected exception is left to propagate
as a traceback on stderr with a non-zero exit — a bug, not an expected
error a caller should parse.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from pipeline.assemble.assemble import AssembleError, assemble_clip
from pipeline.fetch.fetch_clip import FetchError, fetch_clip
from pipeline.format.validate import FormatError, validate_format
from pipeline.orchestrate.auto_finish import auto_finish_job
from pipeline.publish.youtube import YouTubePublishError, upload_video
from pipeline.script.draft import DraftError, start_script_job
from pipeline.voiceover.generate import VoiceoverError, generate_voiceover

_KNOWN_ERRORS = (
    DraftError,
    FetchError,
    VoiceoverError,
    AssembleError,
    FormatError,
    YouTubePublishError,
)


def _select_candidate(args: dict[str, Any]) -> dict[str, Any]:
    job_id, work_dir, prompt = start_script_job(
        args["creator_id"],
        args["batch_id"],
        args["candidate_index"],
        job_id=args.get("job_id"),
    )
    return {"job_id": job_id, "work_dir": str(work_dir), "prompt": prompt}


def _fetch(args: dict[str, Any]) -> dict[str, Any]:
    out_dir = fetch_clip(
        args["creator_id"],
        args["batch_id"],
        args["candidate_index"],
        clip_id=args.get("clip_id"),
        yt_dlp_path=args.get("yt_dlp_path", "yt-dlp"),
    )
    return {"clip_id": out_dir.name, "out_dir": str(out_dir)}


def _voiceover(args: dict[str, Any]) -> dict[str, Any]:
    out_path = generate_voiceover(args["job_id"])
    return {"voiceover_path": str(out_path)}


def _assemble(args: dict[str, Any]) -> dict[str, Any]:
    out_dir = assemble_clip(
        args["job_id"],
        args["creator_id"],
        args["clip_id"],
        ffmpeg_path=args.get("ffmpeg_path", "ffmpeg"),
    )
    return {
        "output_path": str(out_dir / "output.mp4"),
        "manifest_path": str(out_dir / "manifest.json"),
    }


def _check_format(args: dict[str, Any]) -> dict[str, Any]:
    return validate_format(
        args["job_id"],
        args["profile"],
        ffprobe_path=args.get("ffprobe_path", "ffprobe"),
    )


def _auto_finish(args: dict[str, Any]) -> dict[str, Any]:
    """Chains fetch -> voiceover -> assemble -> check-format for a job
    whose script.md already exists (written by script-writer, screened by
    content-reviewer) — see pipeline.orchestrate.auto_finish for why this
    has no separate human-approval gate of its own."""
    return auto_finish_job(
        args["job_id"],
        args["creator_id"],
        args["batch_id"],
        args["candidate_index"],
        args["format_profile"],
        clip_id=args.get("clip_id"),
        yt_dlp_path=args.get("yt_dlp_path", "yt-dlp"),
        ffmpeg_path=args.get("ffmpeg_path", "ffmpeg"),
        ffprobe_path=args.get("ffprobe_path", "ffprobe"),
    )


def _publish_youtube(args: dict[str, Any]) -> dict[str, Any]:
    """The one deliberate, human-triggered exception to this repo's
    "never call a posting/publishing API" rule — see pipeline.publish.youtube
    for the eligibility checks (permission provenance + passed
    format-compliance) this refuses to skip."""
    return upload_video(
        args["job_id"],
        title=args["title"],
        description=args.get("description", ""),
        tags=args.get("tags"),
        category_id=args.get("category_id", "22"),
        privacy_status=args.get("privacy_status", "private"),
    )


_ACTIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "select-candidate": _select_candidate,
    "fetch": _fetch,
    "voiceover": _voiceover,
    "assemble": _assemble,
    "check-format": _check_format,
    "auto-finish": _auto_finish,
    "publish-youtube": _publish_youtube,
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(json.dumps({"ok": False, "error": "usage: <action> <json-args>"}))
        return 2

    action, args_json = argv
    handler = _ACTIONS.get(action)
    if handler is None:
        print(json.dumps({"ok": False, "error": f"unknown action '{action}'"}))
        return 2

    try:
        args = json.loads(args_json)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid JSON args: {exc}"}))
        return 2

    try:
        result = handler(args)
    except _KNOWN_ERRORS as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    except KeyError as exc:
        print(json.dumps({"ok": False, "error": f"missing required field {exc}"}))
        return 2

    print(json.dumps({"ok": True, "result": result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
