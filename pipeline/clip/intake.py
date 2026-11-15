"""Clip-mode source intake.

Takes one long video — a URL (downloaded with yt-dlp) or a local file
(copied) — into raw/clip/<source_id>/ and writes a metadata.json beside it
recording where it came from and on what basis it may be reused.

Unlike commentary mode, the permissions ledger is not a gate here: any
source is accepted. What is safety-critical instead is `rights_basis`,
which every later clip-mode step copies forward:

  allowlist   the platform reports the video's channel as one on the ledger
  own         a local file the owner explicitly marked as his own footage
  unverified  anything else

Nothing here may hand out `allowlist` or `own` without that evidence.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from pipeline.permissions.validate import LedgerError, is_permitted, load_ledger

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_CLIP_DIR = REPO_ROOT / "raw" / "clip"

MAX_DURATION_SECONDS = 3 * 60 * 60

RIGHTS_ALLOWLIST = "allowlist"
RIGHTS_OWN = "own"
RIGHTS_UNVERIFIED = "unverified"
RIGHTS_BASES = (RIGHTS_ALLOWLIST, RIGHTS_OWN, RIGHTS_UNVERIFIED)

# What every later clip-mode file must carry forward from metadata.json.
PROVENANCE_FIELDS = (
    "source_kind",
    "source_url",
    "original_filename",
    "title",
    "uploader",
    "uploader_url",
    "channel_id",
    "channel_url",
    "license",
    "rights_basis",
    "permission_ledger_reference",
)

_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
# Stricter than _SAFE_PATH_COMPONENT, which would let "." and ".." through.
_SOURCE_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")


class IntakeError(ValueError):
    """Raised when a clip-mode source can't be taken in or is rejected."""


def _generate_source_id() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def _new_source_dir(source_id: Optional[str]) -> tuple[str, Path]:
    source_id = source_id or _generate_source_id()
    if not _SOURCE_ID.match(source_id):
        raise IntakeError(
            f"source_id '{source_id}' must look like 20261113T101500Z-a1b2c3"
        )
    source_dir = RAW_CLIP_DIR / source_id
    if source_dir.exists():
        raise IntakeError(f"source directory already exists: {source_dir}")
    source_dir.mkdir(parents=True)
    return source_id, source_dir


def _run(cmd: list[str], tool: str) -> str:
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise IntakeError(f"'{cmd[0]}' not found — is {tool} installed?") from exc
    except subprocess.CalledProcessError as exc:
        raise IntakeError(
            f"{tool} failed (exit {exc.returncode}): {exc.stderr.strip()}"
        ) from exc
    return result.stdout


def _probe_media(path: Path, ffprobe_path: str) -> float:
    """Return the file's duration in seconds, rejecting anything clip mode
    can't use (over the length cap, or nothing to transcribe)."""
    stdout = _run(
        [
            ffprobe_path,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        "ffprobe",
    )
    try:
        probe = json.loads(stdout)
        duration = float(probe["format"]["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise IntakeError(f"could not read a duration from ffprobe for {path.name}") from exc

    _reject_if_too_long(duration)
    streams = probe.get("streams") or []
    if not any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
    ):
        raise IntakeError(f"{path.name} has no audio stream — nothing to transcribe")
    return duration


def _reject_if_too_long(duration_seconds: float) -> None:
    if duration_seconds > MAX_DURATION_SECONDS:
        raise IntakeError(
            f"source is {duration_seconds / 3600:.1f} hours long — the limit is "
            f"{MAX_DURATION_SECONDS // 3600} hours"
        )


def _comparable_host(url: Any) -> Optional[str]:
    if not isinstance(url, str):
        return None
    parsed = urlparse(url.strip())
    # .hostname, not .netloc: "youtube.com@evil.example" must read as evil.example.
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    return host[len("www."):] if host.startswith("www.") else host


def _comparable_channel_url(url: Any) -> Optional[str]:
    """Reduce a channel URL to a form two spellings of the same channel
    share, or None if it can't be compared safely.

    A URL carrying a query, params or fragment is not comparable: dropping
    those would make "/user?id=alice" equal "/user?id=mallory". Only
    YouTube @handles are lowercased — YouTube treats those as
    case-insensitive, but channel IDs (/channel/UC...) are case-sensitive
    and folding them could make two different channels compare equal.
    """
    host = _comparable_host(url)
    if host is None:
        return None
    parsed = urlparse(url.strip())
    if parsed.query or parsed.params or parsed.fragment:
        return None
    segments = [
        segment.lower() if host == "youtube.com" and segment.startswith("@") else segment
        for segment in parsed.path.split("/")
        if segment
    ]
    if not segments:
        return None
    return f"{host}/{'/'.join(segments)}"


def _match_allowlist(info: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return the ledger entry for this video's channel, or None.

    Matches only on the channel URLs the platform itself reports
    (yt-dlp's uploader_url / channel_url), never on the display name —
    anyone can name a channel "NASA" — and never on the URL that was
    pasted in. The video must also live on the ledger entry's own site:
    a page elsewhere can claim any channel URL it likes in its metadata.
    The final yes/no still comes from is_permitted, so an expired entry
    does not count.
    """
    video_host = _comparable_host(info.get("webpage_url"))
    reported = {
        _comparable_channel_url(info.get("uploader_url")),
        _comparable_channel_url(info.get("channel_url")),
    } - {None}
    if video_host is None or not reported:
        return None
    for entry in load_ledger():
        entry_channel = _comparable_channel_url(entry["source_url"])
        if entry_channel is None or entry_channel not in reported:
            continue
        if _comparable_host(entry["source_url"]) != video_host:
            continue
        if is_permitted(entry["creator_id"], entry["source_url"]):
            return entry
    return None


def _fetch_video_info(url: str, yt_dlp_path: str) -> dict[str, Any]:
    stdout = _run(
        [
            yt_dlp_path,
            "--ignore-config",
            "--dump-single-json",
            "--no-playlist",
            # So a pasted channel URL is refused without listing every video on it.
            "--flat-playlist",
            "--no-warnings",
            "--",
            url,
        ],
        "yt-dlp",
    )
    try:
        info = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise IntakeError(f"could not parse yt-dlp's description of {url}") from exc
    if not isinstance(info, dict):
        raise IntakeError(f"yt-dlp returned no video information for {url}")
    if info.get("_type") == "playlist":
        raise IntakeError(f"{url} is a playlist or channel — give one video at a time")
    if info.get("is_live"):
        raise IntakeError(f"{url} is a live stream that hasn't ended")
    return info


def _download_video(info: dict[str, Any], source_dir: Path, yt_dlp_path: str) -> Path:
    """Download the exact video `info` describes.

    yt-dlp is handed the info it already returned rather than the URL
    again: rights_basis was decided from that info, and a second lookup of
    the URL could resolve to a different video (a redirect that changes
    between requests).
    """
    info_path = source_dir / "yt-dlp-info.json"
    info_path.write_text(json.dumps(info), encoding="utf-8")
    try:
        _run(
            [
                yt_dlp_path,
                "--ignore-config",
                "--no-warnings",
                "--load-info-json", str(info_path),
                # A hard 1080p cap; "-S" only sets the preference among what's left.
                "-f", "bv*[height<=1080]+ba/b[height<=1080]",
                "-S", "ext:mp4:m4a",
                "--merge-output-format", "mp4",
                "--remux-video", "mp4",
                "-o", str(source_dir / "source.%(ext)s"),
            ],
            "yt-dlp",
        )
    finally:
        info_path.unlink(missing_ok=True)
    downloaded = sorted(source_dir.glob("source.*"))
    if len(downloaded) != 1:
        raise IntakeError(
            f"expected one downloaded file in {source_dir}, found "
            f"{[p.name for p in downloaded]}"
        )
    return downloaded[0]


def _remove_source_dir(source_dir: Path) -> None:
    shutil.rmtree(source_dir, ignore_errors=True)


def _write_metadata(source_dir: Path, metadata: dict[str, Any]) -> None:
    with (source_dir / "metadata.json").open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")


def load_source(source_id: str) -> tuple[Path, dict[str, Any]]:
    """Return (path to the source video, its metadata) for a finished intake.

    Refuses a source whose provenance is incomplete rather than letting a
    later step fill the gap with a guess.
    """
    if not isinstance(source_id, str) or not _SOURCE_ID.match(source_id):
        raise IntakeError(f"source_id '{source_id}' must look like 20261113T101500Z-a1b2c3")
    source_dir = RAW_CLIP_DIR / source_id
    metadata_path = source_dir / "metadata.json"
    if not metadata_path.is_file():
        raise IntakeError(f"no clip-mode source '{source_id}' — run intake first")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IntakeError(f"{metadata_path} is not valid JSON") from exc
    if not isinstance(metadata, dict) or metadata.get("source_id") != source_id:
        raise IntakeError(f"{metadata_path} does not describe source '{source_id}'")

    missing = [field for field in PROVENANCE_FIELDS if field not in metadata]
    if missing:
        raise IntakeError(f"{metadata_path} is missing provenance field(s): {missing}")
    rights_basis = metadata["rights_basis"]
    if rights_basis not in RIGHTS_BASES:
        raise IntakeError(
            f"{metadata_path} has rights_basis {rights_basis!r}, "
            f"expected one of {list(RIGHTS_BASES)}"
        )
    # The fields must also agree with each other, the way intake writes
    # them — otherwise an edited file could claim a basis it has no
    # evidence for and every later step would repeat it.
    source_kind = metadata["source_kind"]
    if source_kind not in ("url", "file"):
        raise IntakeError(f"{metadata_path} has source_kind {source_kind!r}")
    origin_field = "source_url" if source_kind == "url" else "original_filename"
    if not _string_or_none(metadata[origin_field]):
        raise IntakeError(f"{metadata_path} has no '{origin_field}' — origin unknown")
    if rights_basis == RIGHTS_OWN and source_kind != "file":
        raise IntakeError(
            f"{metadata_path} has rights_basis 'own' on a downloaded source — "
            "only a local file can be marked own"
        )
    if rights_basis == RIGHTS_ALLOWLIST:
        reference = metadata["permission_ledger_reference"]
        if source_kind != "url" or not (
            isinstance(reference, dict) and _string_or_none(reference.get("creator_id"))
        ):
            raise IntakeError(
                f"{metadata_path} has rights_basis 'allowlist' with no "
                "permission_ledger_reference behind it"
            )
        # The reference must be for this video's own channel, the same
        # match intake made — not just any creator who is on the ledger.
        permitted_channel = _comparable_channel_url(reference.get("source_url"))
        recorded_channels = {
            _comparable_channel_url(metadata["uploader_url"]),
            _comparable_channel_url(metadata["channel_url"]),
        } - {None}
        if permitted_channel is None or permitted_channel not in recorded_channels:
            raise IntakeError(
                f"{metadata_path} has rights_basis 'allowlist', but its "
                "permission_ledger_reference is for a different channel than "
                "the one this video came from"
            )

    source_file = metadata.get("source_file")
    if (
        not isinstance(source_file, str)
        or source_file in ("", ".", "..")
        or Path(source_file).name != source_file
    ):
        raise IntakeError(f"{metadata_path} has an invalid 'source_file' value")
    source_path = source_dir / source_file
    if not source_path.is_file():
        raise IntakeError(f"source video missing at {source_path}")
    return source_path, metadata


def provenance(metadata: dict[str, Any]) -> dict[str, Any]:
    """The provenance block to copy, unchanged, into a downstream file."""
    return {field: metadata[field] for field in PROVENANCE_FIELDS}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _string_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def intake_url(
    url: str,
    *,
    source_id: Optional[str] = None,
    yt_dlp_path: str = "yt-dlp",
    ffprobe_path: str = "ffprobe",
) -> Path:
    """Download one video into raw/clip/<source_id>/ and record its provenance."""
    parsed = urlparse(url) if isinstance(url, str) else None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise IntakeError(f"'{url}' is not an http(s) video URL")

    info = _fetch_video_info(url, yt_dlp_path)
    reported_duration = info.get("duration")
    if isinstance(reported_duration, (int, float)):
        # Checked before the download so a 6-hour stream costs nothing.
        _reject_if_too_long(float(reported_duration))

    try:
        ledger_entry = _match_allowlist(info)
    except LedgerError as exc:
        raise IntakeError(f"permissions ledger is unreadable: {exc}") from exc

    source_id, source_dir = _new_source_dir(source_id)
    # BaseException so Ctrl-C mid-download doesn't leave a source without metadata.
    try:
        source_file = _download_video(info, source_dir, yt_dlp_path)
        duration = _probe_media(source_file, ffprobe_path)
        _write_metadata(source_dir, {
            "source_id": source_id,
            "source_kind": "url",
            "source_url": url,
            "original_filename": None,
            "title": _string_or_none(info.get("title")),
            "uploader": _string_or_none(info.get("uploader")),
            "uploader_url": _string_or_none(info.get("uploader_url")),
            "channel_id": _string_or_none(info.get("channel_id")),
            "channel_url": _string_or_none(info.get("channel_url")),
            "license": _string_or_none(info.get("license")),
            "duration_seconds": duration,
            "source_file": source_file.name,
            "fetched_at": _now(),
            "rights_basis": RIGHTS_ALLOWLIST if ledger_entry else RIGHTS_UNVERIFIED,
            "permission_ledger_reference": (
                {
                    "creator_id": ledger_entry["creator_id"],
                    "source_url": ledger_entry["source_url"],
                    "permission_granted_date": ledger_entry["permission_granted_date"],
                }
                if ledger_entry
                else None
            ),
        })
    except BaseException:
        _remove_source_dir(source_dir)
        raise
    return source_dir


def intake_file(
    path: Path | str,
    *,
    own: bool = False,
    source_id: Optional[str] = None,
    ffprobe_path: str = "ffprobe",
) -> Path:
    """Copy one local video into raw/clip/<source_id>/ and record its provenance.

    `own=True` is the owner saying the footage is his. A local file carries
    no other evidence of its origin, so without it the source is unverified.
    """
    # A truthy string like "false" must not be able to claim ownership.
    if not isinstance(own, bool):
        raise IntakeError("own must be True or False")

    # Absolute, so a file named like "-report" can't be read as an ffprobe option.
    original = Path(path).expanduser().resolve()
    if not original.is_file():
        raise IntakeError(f"no file at {original}")

    duration = _probe_media(original, ffprobe_path)

    source_id, source_dir = _new_source_dir(source_id)
    source_file = source_dir / f"source{original.suffix.lower()}"
    try:
        try:
            shutil.copy2(original, source_file)
        except OSError as exc:
            raise IntakeError(f"could not copy {original}: {exc}") from exc
        _write_metadata(source_dir, {
            "source_id": source_id,
            "source_kind": "file",
            "source_url": None,
            "original_filename": original.name,
            "title": original.stem,
            "uploader": None,
            "uploader_url": None,
            "channel_id": None,
            "channel_url": None,
            "license": None,
            "duration_seconds": duration,
            "source_file": source_file.name,
            "fetched_at": _now(),
            "rights_basis": RIGHTS_OWN if own else RIGHTS_UNVERIFIED,
            "permission_ledger_reference": None,
        })
    except BaseException:
        _remove_source_dir(source_dir)
        raise
    return source_dir


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.clip.intake",
        description="Take one long video (URL or local file) into raw/clip/ for clip mode.",
    )
    parser.add_argument("source", help="a video URL, or the path to a local video file")
    parser.add_argument(
        "--own",
        action="store_true",
        help="local files only: this is your own footage (recorded as rights_basis 'own')",
    )
    args = parser.parse_args(argv)

    is_url = urlparse(args.source).scheme in ("http", "https")
    try:
        if is_url:
            if args.own:
                raise IntakeError("--own only applies to local files, not URLs")
            source_dir = intake_url(args.source)
        else:
            source_dir = intake_file(args.source, own=args.own)
    except IntakeError as exc:
        print(f"intake failed: {exc}", file=sys.stderr)
        return 1

    metadata = json.loads((source_dir / "metadata.json").read_text(encoding="utf-8"))
    print(f"source_id:    {metadata['source_id']}")
    print(f"title:        {metadata['title']}")
    print(f"duration:     {metadata['duration_seconds'] / 60:.1f} min")
    print(f"rights_basis: {metadata['rights_basis']}")
    print(f"saved to:     {source_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
