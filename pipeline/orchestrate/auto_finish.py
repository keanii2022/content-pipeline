"""Chains the deterministic post-script stages into one call.

Once a job's script.md exists (written by the script-writer agent) and has
been screened by the content-reviewer agent, everything after that —
fetch, voiceover, assemble, check-format — is deterministic Python with no
agent and no judgment call left in it: `load_script` already enforces the
draft is well-formed (spoken word count, non-empty), and content-reviewer
already screened tone/framing before this is ever called. There is no
further human read-through gate here by design: the human checkpoint for
this creator's clip moves to the control panel's finished list, where a
person skims the staged output (title, thumbnail, source) and decides
whether to post it — not to a per-script approval that blocks the pipeline
from finishing.

This function is intentionally just a thin sequence of the same four
functions run_single.py's CLI and pipeline.control.api already expose
individually — no new pipeline logic, so there is still exactly one
implementation of each stage. It exists so a batch of jobs can be pushed
straight through to `staged/` (and therefore into the control panel)
without a human clicking fetch, then voiceover, then assemble, then
check-format, and waiting on each one.
"""

from __future__ import annotations

from typing import Any, Optional

from pipeline.assemble.assemble import AssembleError, assemble_clip
from pipeline.fetch.fetch_clip import FetchError, fetch_clip
from pipeline.format.validate import FormatError, validate_format
from pipeline.script.draft import DraftError, load_script
from pipeline.voiceover.generate import VoiceoverError, generate_voiceover

AutoFinishError = (DraftError, FetchError, VoiceoverError, AssembleError, FormatError)


def auto_finish_job(
    job_id: str,
    creator_id: str,
    batch_id: str,
    candidate_index: int,
    format_profile: str,
    *,
    clip_id: Optional[str] = None,
    yt_dlp_path: str = "yt-dlp",
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
) -> dict[str, Any]:
    """Run fetch -> voiceover -> assemble -> check-format for one job,
    stopping and reporting exactly which stage got through if any stage
    raises. Requires work/<job_id>/script.md to already exist and pass
    load_script's well-formedness check (raises DraftError otherwise,
    before anything is fetched) — a job whose script-writer/content-reviewer
    pass never happened, or produced something malformed, does not get
    auto-finished.
    """
    # Fails loudly, before any fetch/voiceover work starts, if the script
    # isn't there or isn't well-formed — this is the one gate auto-finish
    # keeps; it is not a human-approval gate, just a sanity check.
    load_script(job_id)

    out_dir = fetch_clip(
        creator_id, batch_id, candidate_index, clip_id=clip_id, yt_dlp_path=yt_dlp_path
    )
    resolved_clip_id = out_dir.name

    voiceover_path = generate_voiceover(job_id)

    assembled_dir = assemble_clip(
        job_id, creator_id, resolved_clip_id, ffmpeg_path=ffmpeg_path
    )

    format_result = validate_format(job_id, format_profile, ffprobe_path=ffprobe_path)

    return {
        "job_id": job_id,
        "clip_id": resolved_clip_id,
        "voiceover_path": str(voiceover_path),
        "output_path": str(assembled_dir / "output.mp4"),
        "manifest_path": str(assembled_dir / "manifest.json"),
        "format_profile": format_profile,
        "format_passed": format_result["passed"],
        "format_reasons": format_result["reasons"],
    }
