"""Commentary script drafting for a single, human-selected candidate.

This module never writes a script itself. The actual drafting is done by
the `script-writer` agent (~/.claude/agents/script-writer.md), which is
scoped to research and write only, and saves its draft to
work/<job_id>/script.md per its own instructions. This module supplies the
scoped prompt for that agent (`build_script_prompt`), sets up the job
directory, and validates the resulting draft is well-formed
(`load_script`) once the agent — and then the `content-reviewer` agent —
have run. The draft is never final: only a human-approved edit of
script.md is used as input to Step 6.
"""

from __future__ import annotations

import datetime
import json
import re
import secrets
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from pipeline.discover.find_candidates import DiscoveryError, get_creator

REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATES_DIR = REPO_ROOT / "data" / "candidates"
WORK_DIR = REPO_ROOT / "work"

_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_MIN_WORDS = 75
_MAX_WORDS = 200


class DraftError(ValueError):
    """Raised when a candidate lookup, permission check, or script draft is invalid."""


def _validate_path_component(value: str, label: str) -> None:
    if not _SAFE_PATH_COMPONENT.match(value):
        raise DraftError(
            f"{label} '{value}' must contain only letters, digits, '.', '_', or '-'"
        )


def _generate_job_id() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def _require_string_field(candidate: dict[str, Any], field: str, index: int) -> str:
    value = candidate.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DraftError(f"candidate at index {index} has no valid '{field}'")
    return value


def _validate_candidate_domain(url: str, creator: dict[str, Any], index: int) -> None:
    """Re-check, at the point a candidate's details are embedded in an
    agent-facing prompt, that its URL is on the permitted creator's domain
    — the same invariant discovery enforces before persisting a batch,
    re-verified here in case the batch file was hand-edited or produced by
    something other than pipeline.discover.find_candidates."""
    candidate_host = urlparse(url).netloc.lower()
    creator_host = urlparse(creator["source_url"]).netloc.lower()
    if not candidate_host or candidate_host != creator_host:
        raise DraftError(
            f"candidate at index {index}.url '{url}' is not on the permitted "
            f"creator's domain ({creator_host}) — refusing to draft a script from it"
        )


def load_candidate(creator_id: str, batch_id: str, candidate_index: int) -> dict[str, Any]:
    """Load one candidate from a Step 2 discovery batch file, re-validating
    that its creator is still on the permissions ledger and that the
    candidate itself is well-formed and on the creator's domain (permission
    is re-checked at every step that reads a candidate, not just at
    discovery time — the batch file may have been hand-edited since)."""
    _validate_path_component(creator_id, "creator_id")
    try:
        creator = get_creator(creator_id)
    except DiscoveryError as exc:
        raise DraftError(str(exc)) from exc

    _validate_path_component(batch_id, "batch_id")
    batch_path = CANDIDATES_DIR / creator_id / f"{batch_id}.json"
    if not batch_path.exists():
        raise DraftError(f"candidate batch not found at {batch_path}")

    with batch_path.open("r", encoding="utf-8") as f:
        batch = json.load(f)

    if batch.get("creator_id") != creator_id:
        raise DraftError(
            f"batch file {batch_path} is for creator '{batch.get('creator_id')}', "
            f"not '{creator_id}'"
        )

    candidates = batch.get("candidates")
    if not isinstance(candidates, list) or not (0 <= candidate_index < len(candidates)):
        count = len(candidates) if isinstance(candidates, list) else 0
        raise DraftError(
            f"candidate_index {candidate_index} is out of range for batch "
            f"'{batch_id}' ({count} candidates)"
        )

    candidate = candidates[candidate_index]
    url = _require_string_field(candidate, "url", candidate_index)
    _require_string_field(candidate, "start_timestamp", candidate_index)
    _require_string_field(candidate, "end_timestamp", candidate_index)
    _require_string_field(candidate, "rationale", candidate_index)
    _validate_candidate_domain(url, creator, candidate_index)

    return {**candidate, "creator": creator}


def build_script_prompt(creator_id: str, batch_id: str, candidate_index: int, job_id: str) -> str:
    """Build the scoped prompt for the `script-writer` agent, limited to
    this one selected candidate's own metadata (url/timestamps/rationale)
    — not the full discovery batch, and not an invitation to pick a
    different candidate."""
    candidate = load_candidate(creator_id, batch_id, candidate_index)
    creator = candidate["creator"]
    return (
        f"Draft a short-form commentary script based on the following "
        f"human-selected clip from {creator['display_name']} "
        f"({creator['platform']}):\n"
        f"- Source URL: {candidate['url']}\n"
        f"- Timestamp range: {candidate['start_timestamp']} - {candidate['end_timestamp']}\n"
        f"- Rationale for selection: {candidate['rationale']}\n\n"
        "Research the actual current state of whatever narrative or story "
        "this clip relates to before writing. Save the draft to "
        f"work/{job_id}/script.md."
    )


def start_script_job(
    creator_id: str,
    batch_id: str,
    candidate_index: int,
    *,
    job_id: Optional[str] = None,
) -> tuple[str, Path, str]:
    """Validate the selected candidate, create its job directory under
    work/, and return (job_id, work_dir, prompt) for the script-writer
    agent to be run against. Raises DraftError, rather than creating a job,
    if the candidate's creator is not currently permitted or the job
    directory already exists."""
    job_id = job_id or _generate_job_id()
    _validate_path_component(job_id, "job_id")

    work_dir = WORK_DIR / job_id
    if work_dir.exists():
        raise DraftError(f"job directory already exists: {work_dir}")

    prompt = build_script_prompt(creator_id, batch_id, candidate_index, job_id)
    work_dir.mkdir(parents=True)
    return job_id, work_dir, prompt


def load_script(job_id: str) -> str:
    """Load and sanity-check the script drafted for job_id, once the
    script-writer agent (and then the content-reviewer agent) have run.
    Only checks the draft is well-formed (non-empty, roughly the spoken
    length script-writer targets) — it does not judge framing or tone,
    that is the content-reviewer agent's job, and it does not decide the
    script is final, that is the human's job."""
    _validate_path_component(job_id, "job_id")
    script_path = WORK_DIR / job_id / "script.md"
    if not script_path.exists():
        raise DraftError(f"no script found at {script_path}")

    text = script_path.read_text(encoding="utf-8").strip()
    if not text:
        raise DraftError(f"script at {script_path} is empty")

    word_count = len(text.split())
    if not (_MIN_WORDS <= word_count <= _MAX_WORDS):
        raise DraftError(
            f"script at {script_path} is {word_count} words, expected "
            f"roughly {_MIN_WORDS}-{_MAX_WORDS} for short-form spoken commentary"
        )

    return text
