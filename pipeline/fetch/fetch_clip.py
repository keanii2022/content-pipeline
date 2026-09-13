"""Single-clip fetch tool.

Takes one candidate you selected from a Step 2 discovery batch
(data/candidates/<creator_id>/<batch_id>.json), re-validates that creator
against the permissions ledger, downloads that one clip with yt-dlp, and
writes it plus a metadata.json into raw/<creator_id>/<clip_id>/.

Fetching a clip whose creator is not (or no longer) on the permissions
ledger must fail loudly, not warn-and-continue.
"""

from __future__ import annotations

import datetime
import json
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from pipeline.discover.find_candidates import DiscoveryError, get_creator

REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATES_DIR = REPO_ROOT / "data" / "candidates"
RAW_DIR = REPO_ROOT / "raw"

_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


class FetchError(ValueError):
    """Raised when a candidate lookup, permission check, or download fails."""


def _validate_path_component(value: str, label: str) -> None:
    if not _SAFE_PATH_COMPONENT.match(value):
        raise FetchError(
            f"{label} '{value}' must contain only letters, digits, '.', '_', or '-'"
        )


def _generate_clip_id() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def load_candidate(creator_id: str, batch_id: str, candidate_index: int) -> dict[str, Any]:
    """Load one candidate from a Step 2 discovery batch file."""
    _validate_path_component(creator_id, "creator_id")
    _validate_path_component(batch_id, "batch_id")

    batch_path = CANDIDATES_DIR / creator_id / f"{batch_id}.json"
    if not batch_path.exists():
        raise FetchError(f"candidate batch not found at {batch_path}")

    with batch_path.open("r", encoding="utf-8") as f:
        batch = json.load(f)

    if batch.get("creator_id") != creator_id:
        raise FetchError(
            f"batch file {batch_path} is for creator '{batch.get('creator_id')}', "
            f"not '{creator_id}'"
        )

    candidates = batch.get("candidates")
    if not isinstance(candidates, list) or not (0 <= candidate_index < len(candidates)):
        count = len(candidates) if isinstance(candidates, list) else 0
        raise FetchError(
            f"candidate_index {candidate_index} is out of range for batch "
            f"'{batch_id}' ({count} candidates)"
        )

    return candidates[candidate_index]


def _require_string_field(candidate: dict[str, Any], field: str, index: int) -> str:
    value = candidate.get(field)
    if not isinstance(value, str) or not value.strip():
        raise FetchError(f"candidate at index {index} has no valid '{field}'")
    return value


def _validate_candidate_domain(url: str, creator: dict[str, Any], index: int) -> None:
    """Re-check, at the point media is actually fetched, that the candidate's
    URL is on the permitted creator's domain — the same invariant discovery
    enforces before persisting a batch, re-verified here in case the batch
    file was hand-edited or produced by something other than
    pipeline.discover.find_candidates.
    """
    candidate_host = urlparse(url).netloc.lower()
    creator_host = urlparse(creator["source_url"]).netloc.lower()
    if not candidate_host or candidate_host != creator_host:
        raise FetchError(
            f"candidate at index {index}.url '{url}' is not on the permitted "
            f"creator's domain ({creator_host}) — refusing to fetch"
        )


def _download(url: str, start: str, end: str, out_dir: Path, yt_dlp_path: str) -> Path:
    output_template = str(out_dir / "clip.%(ext)s")
    cmd = [
        yt_dlp_path,
        "--download-sections",
        f"*{start}-{end}",
        "--force-keyframes-at-cuts",
        "-o",
        output_template,
        url,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise FetchError(f"'{yt_dlp_path}' not found — is yt-dlp installed?") from exc
    except subprocess.CalledProcessError as exc:
        raise FetchError(
            f"yt-dlp failed for {url} (exit {exc.returncode}): {exc.stderr.strip()}"
        ) from exc

    downloaded = sorted(out_dir.glob("clip.*"))
    if not downloaded:
        raise FetchError(f"yt-dlp reported success but no output file found in {out_dir}")
    if len(downloaded) > 1:
        raise FetchError(
            f"yt-dlp produced multiple output files in {out_dir}: "
            f"{[p.name for p in downloaded]}"
        )
    return downloaded[0]


def fetch_clip(
    creator_id: str,
    batch_id: str,
    candidate_index: int,
    *,
    clip_id: Optional[str] = None,
    yt_dlp_path: str = "yt-dlp",
) -> Path:
    """Download exactly one candidate clip into raw/<creator_id>/<clip_id>/.

    Re-validates permission immediately before downloading (not just at
    discovery time), since this is the point actual media gets fetched.
    Raises FetchError, rather than downloading, if the creator is not
    currently on the permissions ledger.
    """
    try:
        creator = get_creator(creator_id)
    except DiscoveryError as exc:
        raise FetchError(str(exc)) from exc

    candidate = load_candidate(creator_id, batch_id, candidate_index)
    source_url = _require_string_field(candidate, "url", candidate_index)
    start_timestamp = _require_string_field(candidate, "start_timestamp", candidate_index)
    end_timestamp = _require_string_field(candidate, "end_timestamp", candidate_index)
    _validate_candidate_domain(source_url, creator, candidate_index)

    clip_id = clip_id or _generate_clip_id()
    _validate_path_component(clip_id, "clip_id")

    out_dir = RAW_DIR / creator_id / clip_id
    if out_dir.exists():
        raise FetchError(f"clip directory already exists: {out_dir}")
    out_dir.mkdir(parents=True)

    try:
        downloaded_path = _download(source_url, start_timestamp, end_timestamp, out_dir, yt_dlp_path)
    except FetchError:
        for leftover in out_dir.iterdir():
            leftover.unlink()
        out_dir.rmdir()
        raise

    metadata = {
        "clip_id": clip_id,
        "creator_id": creator_id,
        "source_url": source_url,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "rationale": candidate.get("rationale"),
        "downloaded_file": downloaded_path.name,
        "permission_ledger_reference": {
            "creator_id": creator["creator_id"],
            "source_url": creator["source_url"],
            "permission_granted_date": creator["permission_granted_date"],
        },
        "candidate_batch_id": batch_id,
        "candidate_index": candidate_index,
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with (out_dir / "metadata.json").open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")

    return out_dir
