"""Platform format-compliance validation for staged output.

Checks a staged job's output file (from pipeline.assemble.assemble)
against a named pipeline.format.profiles.FormatProfile using ffprobe, and
records the pass/fail result plus reasons into that job's manifest.json
under a "format_compliance" key. Validation only — never re-encodes or
otherwise fixes non-compliant output.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from typing import Any

from pipeline.format.profiles import PROFILES, FormatProfile
from pipeline.script.draft import _SAFE_PATH_COMPONENT

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGED_DIR = REPO_ROOT / "staged"

_ASPECT_RATIO_TOLERANCE = 0.02


class FormatError(ValueError):
    """Raised when the profile is unknown, the manifest/output is missing,
    or ffprobe fails or returns unparseable output."""


def _validate_path_component(value: str, label: str) -> None:
    if not _SAFE_PATH_COMPONENT.match(value):
        raise FormatError(
            f"{label} '{value}' must contain only letters, digits, '.', '_', or '-'"
        )


def _get_profile(profile_name: str) -> FormatProfile:
    try:
        return PROFILES[profile_name]
    except KeyError:
        raise FormatError(
            f"unknown format profile '{profile_name}' — choices are {sorted(PROFILES)}"
        ) from None


def _load_manifest(job_id: str) -> tuple[Path, dict[str, Any]]:
    manifest_path = STAGED_DIR / job_id / "manifest.json"
    if not manifest_path.exists():
        raise FormatError(f"no staged manifest found at {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as f:
        return manifest_path, json.load(f)


def _locate_output_file(manifest_path: Path, output_file: str) -> Path:
    job_dir = manifest_path.parent
    output_path = (job_dir / output_file).resolve()
    if job_dir.resolve() not in output_path.parents:
        raise FormatError(
            f"manifest 'output_file' {output_file!r} escapes the job directory {job_dir}"
        )
    if not output_path.exists():
        raise FormatError(f"staged output not found at {output_path}")
    return output_path


def _probe(output_path: Path, ffprobe_path: str) -> dict[str, Any]:
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(output_path),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise FormatError(
            f"'{ffprobe_path}' not found — is ffmpeg (ffprobe) installed?"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise FormatError(
            f"ffprobe failed (exit {exc.returncode}): {exc.stderr.strip()}"
        ) from exc
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FormatError(f"could not parse ffprobe output: {exc}") from exc


def _check_against_profile(probe_data: dict[str, Any], profile: FormatProfile) -> list[str]:
    reasons: list[str] = []

    streams = probe_data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    format_info = probe_data.get("format", {})

    if video_stream is None:
        reasons.append("no video stream found")
    else:
        width = video_stream.get("width")
        height = video_stream.get("height")
        if not width or not height:
            reasons.append("video stream missing width/height")
        else:
            expected_ratio = profile.aspect_ratio[0] / profile.aspect_ratio[1]
            actual_ratio = width / height
            if abs(actual_ratio - expected_ratio) > _ASPECT_RATIO_TOLERANCE:
                reasons.append(
                    f"aspect ratio {width}:{height} does not match required "
                    f"{profile.aspect_ratio[0]}:{profile.aspect_ratio[1]}"
                )
        if video_stream.get("codec_name") != profile.video_codec:
            reasons.append(
                f"video codec '{video_stream.get('codec_name')}' does not match "
                f"required '{profile.video_codec}'"
            )

    if audio_stream is None:
        reasons.append("no audio stream found")
    elif audio_stream.get("codec_name") != profile.audio_codec:
        reasons.append(
            f"audio codec '{audio_stream.get('codec_name')}' does not match "
            f"required '{profile.audio_codec}'"
        )

    duration_str = format_info.get("duration")
    if duration_str is None:
        reasons.append("no duration found in ffprobe output")
    else:
        try:
            duration = float(duration_str)
        except (TypeError, ValueError):
            reasons.append(f"ffprobe reported a non-numeric duration: {duration_str!r}")
        else:
            if duration > profile.max_duration_seconds:
                reasons.append(
                    f"duration {duration:.2f}s exceeds max {profile.max_duration_seconds}s "
                    f"for profile '{profile.name}'"
                )

    return reasons


def validate_format(
    job_id: str, profile_name: str, *, ffprobe_path: str = "ffprobe"
) -> dict[str, Any]:
    """Check the staged output for job_id against the named format
    profile, write the result into that job's manifest.json under
    "format_compliance", and return the result dict. Raises FormatError
    if the profile is unknown, the manifest or output file is missing, or
    ffprobe fails. Validation only — never re-encodes or modifies output."""
    _validate_path_component(job_id, "job_id")
    profile = _get_profile(profile_name)
    manifest_path, manifest = _load_manifest(job_id)

    output_file = manifest.get("output_file")
    if not output_file:
        raise FormatError(f"manifest at {manifest_path} has no 'output_file'")
    output_path = _locate_output_file(manifest_path, output_file)

    probe_data = _probe(output_path, ffprobe_path)
    reasons = _check_against_profile(probe_data, profile)

    result = {
        "profile": profile.name,
        "passed": len(reasons) == 0,
        "reasons": reasons,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    manifest["format_compliance"] = result
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    return result
