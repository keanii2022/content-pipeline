"""Single-clip assembly: combine one raw clip + one voiceover into output.

Takes the raw clip fetched by Step 4 (raw/<creator_id>/<clip_id>/) and the
voiceover audio synthesized by Step 6 from the human-approved script
(work/<job_id>/), and combines them with ffmpeg into one 9:16 short-form
video at staged/<job_id>/output.mp4, plus a manifest.json recording the
source clip, the permissions-ledger reference, and the approved script
used. Re-validates the clip's creator is still on the permissions ledger
immediately before assembly, the same "check again at the point of use"
rule Steps 4 and 5 follow.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from typing import Any

from pipeline.discover.find_candidates import DiscoveryError, get_creator
from pipeline.script.draft import _SAFE_PATH_COMPONENT, load_script

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "raw"
WORK_DIR = REPO_ROOT / "work"
STAGED_DIR = REPO_ROOT / "staged"

_OUTPUT_WIDTH = 1080
_OUTPUT_HEIGHT = 1920


class AssembleError(ValueError):
    """Raised when the raw clip, voiceover, or ffmpeg assembly is invalid."""


def _validate_path_component(value: str, label: str) -> None:
    if not _SAFE_PATH_COMPONENT.match(value):
        raise AssembleError(
            f"{label} '{value}' must contain only letters, digits, '.', '_', or '-'"
        )


def load_raw_clip_metadata(creator_id: str, clip_id: str) -> dict[str, Any]:
    """Load the metadata.json Step 4 wrote for this raw clip."""
    clip_dir = RAW_DIR / creator_id / clip_id
    metadata_path = clip_dir / "metadata.json"
    if not metadata_path.exists():
        raise AssembleError(f"no raw clip metadata found at {metadata_path}")

    with metadata_path.open("r", encoding="utf-8") as f:
        metadata = json.load(f)

    if metadata.get("creator_id") != creator_id or metadata.get("clip_id") != clip_id:
        raise AssembleError(
            f"metadata at {metadata_path} is for "
            f"creator_id={metadata.get('creator_id')!r} clip_id={metadata.get('clip_id')!r}, "
            f"not creator_id={creator_id!r} clip_id={clip_id!r}"
        )

    return metadata


def _locate_clip_file(creator_id: str, clip_id: str, metadata: dict[str, Any]) -> Path:
    downloaded_file = metadata.get("downloaded_file")
    if not isinstance(downloaded_file, str) or not downloaded_file:
        raise AssembleError(f"raw clip metadata for '{clip_id}' has no 'downloaded_file'")

    clip_path = RAW_DIR / creator_id / clip_id / downloaded_file
    if not clip_path.exists():
        raise AssembleError(f"raw clip file not found at {clip_path}")
    return clip_path


def _locate_voiceover_file(job_id: str) -> Path:
    matches = sorted((WORK_DIR / job_id).glob("voiceover.*"))
    if not matches:
        raise AssembleError(f"no voiceover audio found in {WORK_DIR / job_id}")
    if len(matches) > 1:
        raise AssembleError(
            f"multiple voiceover files found in {WORK_DIR / job_id}: "
            f"{[p.name for p in matches]}"
        )
    return matches[0]


def _run_ffmpeg(clip_path: Path, voiceover_path: Path, output_path: Path, ffmpeg_path: str) -> None:
    scale_pad = (
        f"scale={_OUTPUT_WIDTH}:{_OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={_OUTPUT_WIDTH}:{_OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )
    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        str(clip_path),
        "-i",
        str(voiceover_path),
        "-filter_complex",
        f"[0:v]{scale_pad}[v]",
        "-map",
        "[v]",
        "-map",
        "1:a",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-shortest",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise AssembleError(f"'{ffmpeg_path}' not found — is ffmpeg installed?") from exc
    except subprocess.CalledProcessError as exc:
        raise AssembleError(
            f"ffmpeg failed (exit {exc.returncode}): {exc.stderr.strip()}"
        ) from exc


def assemble_clip(
    job_id: str,
    creator_id: str,
    clip_id: str,
    *,
    ffmpeg_path: str = "ffmpeg",
) -> Path:
    """Combine the raw clip at raw/<creator_id>/<clip_id>/ with the
    voiceover at work/<job_id>/ into staged/<job_id>/output.mp4, plus a
    manifest.json. Re-validates the clip's creator is still permitted
    before assembly, the first check performed, matching the convention in
    Steps 4 and 5. Raises AssembleError, rather than staging anything, if
    the creator's permission has lapsed, the raw clip or voiceover is
    missing, or staged output already exists for this job; propagates
    ffmpeg failures as AssembleError, and DraftError (from load_script) if
    no approved script exists yet for this job.
    """
    _validate_path_component(job_id, "job_id")
    _validate_path_component(creator_id, "creator_id")
    _validate_path_component(clip_id, "clip_id")

    try:
        creator = get_creator(creator_id)
    except DiscoveryError as exc:
        raise AssembleError(str(exc)) from exc

    out_dir = STAGED_DIR / job_id
    if out_dir.exists():
        raise AssembleError(f"staged output already exists: {out_dir}")

    clip_metadata = load_raw_clip_metadata(creator_id, clip_id)
    clip_path = _locate_clip_file(creator_id, clip_id, clip_metadata)
    voiceover_path = _locate_voiceover_file(job_id)
    script_text = load_script(job_id)

    out_dir.mkdir(parents=True)
    output_path = out_dir / "output.mp4"

    try:
        _run_ffmpeg(clip_path, voiceover_path, output_path, ffmpeg_path)
    except AssembleError:
        for leftover in out_dir.iterdir():
            leftover.unlink()
        out_dir.rmdir()
        raise

    manifest = {
        "job_id": job_id,
        "output_file": output_path.name,
        "source_clip": {
            "creator_id": creator_id,
            "clip_id": clip_id,
            "source_url": clip_metadata.get("source_url"),
            "start_timestamp": clip_metadata.get("start_timestamp"),
            "end_timestamp": clip_metadata.get("end_timestamp"),
        },
        "permission_ledger_reference": {
            "creator_id": creator["creator_id"],
            "source_url": creator["source_url"],
            "permission_granted_date": creator["permission_granted_date"],
        },
        "approved_script": script_text,
        "voiceover_file": voiceover_path.name,
        "assembled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with (out_dir / "manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    return out_dir
