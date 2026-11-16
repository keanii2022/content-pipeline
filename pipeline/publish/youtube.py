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
  - the job's staged/<job_id>/manifest.json exists and records where the
    clip came from. Publishing a job with no recorded provenance is
    refused, not warned-and-continued, same convention as
    pipeline.fetch.fetch_clip. What counts depends on the mode:
      - commentary mode: a permissions-ledger reference (the same
        provenance chain every other stage re-checks).
      - clip mode: the source's provenance block with its `rights_basis`.
        `allowlist` is re-checked against the ledger as it stands now,
        `own` passes, and `unverified` passes only if the person
        publishing acknowledges it in this very call
        (`acknowledge_unverified_rights=True`). That acknowledgment is
        written to the manifest with a timestamp before the upload starts
        and never carries over to a later upload.
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

import contextlib
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

from pipeline.clip.cut import MODE as CLIP_MODE
from pipeline.clip.intake import (
    RIGHTS_ALLOWLIST,
    RIGHTS_UNVERIFIED,
    IntakeError,
    SourceNotFoundError,
    check_provenance,
    load_source_record,
    provenance,
)
from pipeline.permissions.validate import LedgerError, is_permitted

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGED_DIR = REPO_ROOT / "staged"
CLIENT_SECRETS_PATH = REPO_ROOT / "credentials.json"
TOKEN_PATH = REPO_ROOT / "data" / "youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_VALID_PRIVACY = {"private", "unlisted", "public"}

# The control panel's checkbox uses these exact words (a test keeps the
# two in step), so a refusal here names the box the person has to tick.
UNVERIFIED_ACK_LABEL = "I've confirmed I have the right to post this clip"
UNVERIFIED_ACK_FLAG = "--acknowledge-unverified-rights"
ACKNOWLEDGMENT_FIELD = "unverified_rights_acknowledgment"


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
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise YouTubePublishError(f"{manifest_path} is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise YouTubePublishError(f"{manifest_path} is not a job manifest")
    return manifest


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


# Keys only pipeline.clip.cut writes; pipeline.assemble.assemble writes none
# of them. review-app/public/app.js keeps the same list (a test checks).
CLIP_JOB_KEYS = ("mode", "source", "source_id", "highlight")


def _is_clip_job(manifest: dict[str, Any]) -> bool:
    # Any trace of clip mode puts a manifest under the clip-mode rules, so
    # dropping `mode` or emptying `source` and adding a ledger reference at
    # the top level can't stand in for the recorded rights_basis.
    return any(key in manifest for key in CLIP_JOB_KEYS)


def _check_clip_rights(
    job_id: str, manifest: dict[str, Any], acknowledge_unverified_rights: bool
) -> str:
    """Return the clip's rights_basis, or refuse. Never rewrites it."""
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise YouTubePublishError(
            f"job '{job_id}' is a clip-mode job with no recorded source "
            "provenance — refusing to publish a clip with no provenance"
        )
    try:
        check_provenance(source, f"job '{job_id}' manifest's source block")
        try:
            _, metadata = load_source_record(manifest.get("source_id"))
        except SourceNotFoundError:
            metadata = None
    except IntakeError as exc:
        raise YouTubePublishError(
            f"refusing to publish job '{job_id}': its provenance can't be relied on — {exc}"
        ) from exc
    if metadata is not None and provenance(metadata) != source:
        raise YouTubePublishError(
            f"refusing to publish job '{job_id}': the provenance in its manifest "
            f"no longer matches the source it was cut from "
            f"(raw/clip/{manifest.get('source_id')}/metadata.json)"
        )

    rights_basis = source["rights_basis"]
    # With the source record gone, the manifest is the only thing saying
    # where this clip came from, and it can't vouch for itself: `own` and
    # `allowlist` need the record behind them. `unverified` claims nothing,
    # so it can still go out on the person's acknowledgment below.
    if metadata is None and rights_basis != RIGHTS_UNVERIFIED:
        raise YouTubePublishError(
            f"refusing to publish job '{job_id}': it is labelled "
            f"'{rights_basis}', but the record of the source it was cut from "
            f"(raw/clip/{manifest.get('source_id')}/metadata.json) is gone, so "
            "that label can't be checked. The source video can be deleted; "
            "its metadata.json has to stay."
        )
    if rights_basis == RIGHTS_ALLOWLIST:
        reference = source["permission_ledger_reference"]
        creator_id = reference["creator_id"]
        try:
            permitted = is_permitted(creator_id, reference.get("source_url"))
        except LedgerError as exc:
            raise YouTubePublishError(
                f"could not re-check the permissions ledger for job '{job_id}': {exc}"
            ) from exc
        if not permitted:
            raise YouTubePublishError(
                f"job '{job_id}' was cut as rights_basis 'allowlist' for "
                f"'{creator_id}', but the ledger no longer permits it (entry "
                "removed, changed or expired) — refusing to publish"
            )
    # `is not True`, not falsiness: only a real yes from the person
    # publishing counts, never a stray string or 1 from a caller.
    if rights_basis == RIGHTS_UNVERIFIED and acknowledge_unverified_rights is not True:
        raise YouTubePublishError(
            f"job '{job_id}' was cut from a source whose reuse rights are "
            "unverified. Publishing it needs your say-so for this upload: tick "
            f"\"{UNVERIFIED_ACK_LABEL}\" in the control panel, or add "
            f"{UNVERIFIED_ACK_FLAG} on the command line"
        )
    return rights_basis


def _check_eligibility(
    job_id: str,
    manifest: dict[str, Any],
    *,
    acknowledge_unverified_rights: bool = False,
) -> Optional[str]:
    """Refuse to publish a job with no recorded permission provenance, or
    that hasn't passed format-compliance — mirrors the fail-loudly-not-
    warn-and-continue convention every other stage in this pipeline uses
    for its own safety-critical checks. Field names match the manifest
    schema pipeline.assemble.assemble, pipeline.clip.cut and
    pipeline.format.validate actually write (see
    staged/<job_id>/manifest.json).

    Returns the rights_basis of a clip-mode job, None for a commentary job."""
    if _is_clip_job(manifest):
        rights_basis: Optional[str] = _check_clip_rights(
            job_id, manifest, acknowledge_unverified_rights
        )
    else:
        rights_basis = None
        if not manifest.get("permission_ledger_reference"):
            raise YouTubePublishError(
                f"job '{job_id}' manifest has no recorded permission_ledger_reference "
                "— refusing to publish a clip with no provenance"
            )
    format_compliance = manifest.get("format_compliance")
    passed = format_compliance.get("passed") if isinstance(format_compliance, dict) else None
    if passed is not True:
        raise YouTubePublishError(
            f"job '{job_id}' has not passed format-compliance "
            f"(format_compliance.passed={passed!r}) — "
            "run check-format (or auto-finish) first"
        )
    return rights_basis


def _record_acknowledgment(job_id: str) -> None:
    """Write the person's acknowledgment into the manifest, with the time.

    It is a record of what was said for one upload, not a standing
    permission: _check_clip_rights never reads it back.
    """
    manifest_path = STAGED_DIR / job_id / "manifest.json"
    # Read again rather than reuse the copy from before the login, which
    # can take minutes: the control panel may have saved a review note to
    # this file meanwhile. Whatever is there now has to pass the gate too.
    manifest = _load_manifest(job_id)
    _check_eligibility(job_id, manifest, acknowledge_unverified_rights=True)
    manifest[ACKNOWLEDGMENT_FIELD] = {
        "acknowledged": True,
        "acknowledged_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    partial_path = manifest_path.with_name("manifest.json.partial")
    try:
        partial_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(partial_path, manifest_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            partial_path.unlink()
        raise YouTubePublishError(
            f"could not record the rights acknowledgment for job '{job_id}' "
            f"({exc}) — nothing was uploaded"
        ) from exc


def _get_authenticated_service():
    """Builds an authenticated YouTube Data API client, running the
    one-time interactive OAuth consent flow if no cached token exists yet.
    Imports the google-api-python-client / google-auth-oauthlib stack
    lazily so the rest of this module (eligibility checks, path handling)
    stays importable/testable without those packages installed."""
    try:
        from google.auth.exceptions import RefreshError
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
            try:
                creds.refresh(Request())
            except RefreshError:
                # A Google Cloud app left in "Testing" mode has its refresh
                # tokens revoked after 7 days — fall through to a fresh
                # consent flow instead of crashing.
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_PATH), SCOPES)
            # run_local_server prints its "visit this URL" prompt to stdout,
            # which pipeline.control.api reserves for its one JSON result —
            # send it to stderr so the control panel can still parse it.
            with contextlib.redirect_stdout(sys.stderr):
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
    acknowledge_unverified_rights: bool = False,
) -> dict[str, Any]:
    """Upload one staged job's output to YouTube. Requires a human-supplied
    title (there is no auto-generated title path — the point of keeping
    this human-triggered is that a person names what's going out under
    their channel). A clip-mode job whose rights_basis is `unverified`
    also requires `acknowledge_unverified_rights=True`, given for this
    call. Returns {"video_id", "url"} on success."""
    if not title or not title.strip():
        raise YouTubePublishError("title is required and cannot be blank")
    if privacy_status not in _VALID_PRIVACY:
        raise YouTubePublishError(
            f"privacy_status must be one of {sorted(_VALID_PRIVACY)}, got {privacy_status!r}"
        )

    manifest = _load_manifest(job_id)
    rights_basis = _check_eligibility(
        job_id, manifest, acknowledge_unverified_rights=acknowledge_unverified_rights
    )
    output_path = _resolve_output_path(job_id, manifest)

    # _get_authenticated_service() first: it's the one with the try/except
    # that turns a missing google-api-python-client into a clean
    # YouTubePublishError. Importing MediaFileUpload before that ran (as a
    # prior version of this function did) hit an unguarded ImportError
    # instead — this ordering makes sure the friendly error always fires
    # first when the dependency isn't installed.
    youtube = _get_authenticated_service()
    from googleapiclient.http import MediaFileUpload

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

    # Recorded after the login (so a failed sign-in leaves no record of an
    # upload that never began) and before the first byte goes out (so
    # nothing unverified is ever uploaded without the record).
    if rights_basis == RIGHTS_UNVERIFIED:
        _record_acknowledgment(job_id)

    response = None
    while response is None:
        status, response = request.next_chunk()

    video_id = response["id"]
    return {"video_id": video_id, "url": f"https://youtube.com/watch?v={video_id}"}
