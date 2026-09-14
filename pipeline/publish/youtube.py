"""YouTube Data API v3 publishing — the one deliberate, scoped exception to
this repo's "publishing is manual, not automated" boundary (see CLAUDE.md
and docs/pipeline-stages.md).

This module still requires a human to trigger it per video — from the
control panel's "Publish to YouTube" button or the `publish` CLI
subcommand — and a human still supplies the title. What it removes is the
copy-file/open-browser/click-through-YouTube's-uploader busywork: the
actual upload call now goes through the API instead of your hands. It
does NOT auto-publish anything the moment a job is staged or auto-finished
— nothing here is wired to run without that explicit per-video call.

Before uploading, this refuses (raises YouTubePublishError) unless:
  - the job's staged/<job_id>/manifest.json exists and records a
    permissions-ledger reference (the same provenance chain every other
    stage re-checks) — publishing a job with no recorded permission
    reference is refused, not warned-and-continued, same convention as
    pipeline.fetch.fetch_clip.
  - the manifest's format-compliance result is present and passed. A job
    that was never check-format'd, or failed it, is refused.

Auth: OAuth2 "installed app" flow, one-time interactive consent the first
time this runs (opens a local browser tab), then a cached refresh token at
data/youtube_token.json (gitignored) for every call after that. Needs a
Google Cloud OAuth client (Desktop app type) with the YouTube Data API v3
enabled, downloaded as credentials.json in the repo root (already
gitignored, same convention as .env).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGED_DIR = REPO_ROOT / "staged"
CLIENT_SECRETS_PATH = REPO_ROOT / "credentials.json"
TOKEN_PATH = REPO_ROOT / "data" / "youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_VALID_PRIVACY = {"private", "unlisted", "public"}


class YouTubePublishError(RuntimeError):
    """Raised when a job isn't eligible to publish, credentials are
    missing/invalid, or the upload itself fails."""


def _validate_path_component(value: str, label: str) -> None:
    if not _SAFE_PATH_COMPONENT.match(value):
        raise YouTubePublishError(
            f"{label} '{value}' must contain only letters, digits, '.', '_', or '-'"
        )


def _load_manifest(job_id: str) -> dict[str, Any]:
    _validate_path_component(job_id, "job_id")
    manifest_path = STAGED_DIR / job_id / "manifest.json"
    if not manifest_path.exists():
        raise YouTubePublishError(
            f"no staged manifest at {manifest_path} — job must be assembled "
            "(and ideally check-format'd) before it can be published"
        )
    with manifest_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_output_path(job_id: str, manifest: dict[str, Any]) -> Path:
    output_file = manifest.get("output_file")
    if not isinstance(output_file, str) or not output_file:
        raise YouTubePublishError(f"manifest for job '{job_id}' has no 'output_file'")
    job_dir = (STAGED_DIR / job_id).resolve()
    resolved = (STAGED_DIR / job_id / output_file).resolve()
    if job_dir not in resolved.parents and resolved != job_dir:
        raise YouTubePublishError(f"output_file for job '{job_id}' escapes its job directory")
    if not resolved.exists():
        raise YouTubePublishError(f"staged output not found at {resolved}")
    return resolved


def _check_eligibility(job_id: str, manifest: dict[str, Any]) -> None:
    """Refuse to publish a job with no recorded permission provenance, or
    that hasn't passed format-compliance — mirrors the fail-loudly-not-
    warn-and-continue convention every other stage in this pipeline uses
    for its own safety-critical checks. Field names match the manifest
    schema pipeline.assemble.assemble and pipeline.format.validate
    actually write (see staged/<job_id>/manifest.json)."""
    if not manifest.get("permission_ledger_reference"):
        raise YouTubePublishError(
            f"job '{job_id}' manifest has no recorded permission_ledger_reference "
            "— refusing to publish a clip with no provenance"
        )
    format_compliance = manifest.get("format_compliance") or {}
    if format_compliance.get("passed") is not True:
        raise YouTubePublishError(
            f"job '{job_id}' has not passed format-compliance "
            f"(format_compliance.passed={format_compliance.get('passed')!r}) — "
            "run check-format (or auto-finish) first"
        )


def _get_authenticated_service():
    """Builds an authenticated YouTube Data API client, running the
    one-time interactive OAuth consent flow if no cached token exists yet.
    Imports the google-api-python-client / google-auth-oauthlib stack
    lazily so the rest of this module (eligibility checks, path handling)
    stays importable/testable without those packages installed."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise YouTubePublishError(
            "missing YouTube API dependencies — install "
            "google-api-python-client, google-auth-httplib2, and "
            "google-auth-oauthlib (see pyproject.toml's [project.optional-"
            "dependencies].youtube)"
        ) from exc

    if not CLIENT_SECRETS_PATH.exists():
        raise YouTubePublishError(
            f"no OAuth client secrets at {CLIENT_SECRETS_PATH} — download a "
            "Desktop-app OAuth client for a Google Cloud project with the "
            "YouTube Data API v3 enabled, and save it there as credentials.json"
        )

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def upload_video(
    job_id: str,
    *,
    title: str,
    description: str = "",
    tags: Optional[list[str]] = None,
    category_id: str = "22",  # "People & Blogs" — a reasonable default, not load-bearing
    privacy_status: str = "private",
) -> dict[str, Any]:
    """Upload one staged job's output to YouTube. Requires a human-supplied
    title (there is no auto-generated title path — the point of keeping
    this human-triggered is that a person names what's going out under
    their channel). Returns {"video_id", "url"} on success."""
    if not title or not title.strip():
        raise YouTubePublishError("title is required and cannot be blank")
    if privacy_status not in _VALID_PRIVACY:
        raise YouTubePublishError(
            f"privacy_status must be one of {sorted(_VALID_PRIVACY)}, got {privacy_status!r}"
        )

    manifest = _load_manifest(job_id)
    _check_eligibility(job_id, manifest)
    output_path = _resolve_output_path(job_id, manifest)

    from googleapiclient.http import MediaFileUpload

    youtube = _get_authenticated_service()
    body = {
        "snippet": {
            "title": title.strip(),
            "description": description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {"privacyStatus": privacy_status},
    }
    media = MediaFileUpload(str(output_path), chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()

    video_id = response["id"]
    return {"video_id": video_id, "url": f"https://youtube.com/watch?v={video_id}"}
