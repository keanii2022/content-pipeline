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
USED_FACTS_PATH = REPO_ROOT / "data" / "used_facts.json"

_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_MIN_WORDS = 50
_MAX_WORDS = 80
_METADATA_DIVIDER = "---"
_SECTION_TAG_RE = re.compile(r"^\[[A-Z][A-Z ]*\]$")


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


def load_used_facts() -> list[str]:
    """Load the ledger of facts/hooks already used in a prior script, so a
    new draft can avoid repeating one. Returns an empty list if the ledger
    doesn't exist yet (first run) — this is a soft duplicate-avoidance
    aid embedded in the agent's prompt, not a hard validation gate."""
    if not USED_FACTS_PATH.exists():
        return []
    with USED_FACTS_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return [entry["fact"] for entry in data if isinstance(entry, dict) and entry.get("fact")]


def record_used_fact(job_id: str, fact: str) -> None:
    """Append one job's hook/fact to the ledger, once its script has been
    synthesized into a voiceover (the point Step 6 treats as the script
    being final — see pipeline.voiceover.generate)."""
    entries = []
    if USED_FACTS_PATH.exists():
        with USED_FACTS_PATH.open("r", encoding="utf-8") as f:
            entries = json.load(f)
    entries.append({"job_id": job_id, "fact": fact, "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    USED_FACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with USED_FACTS_PATH.open("w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")


def build_script_prompt(creator_id: str, batch_id: str, candidate_index: int, job_id: str) -> str:
    """Build the scoped prompt for the `script-writer` agent, limited to
    this one selected candidate's own metadata (url/timestamps/rationale)
    — not the full discovery batch, and not an invitation to pick a
    different candidate."""
    candidate = load_candidate(creator_id, batch_id, candidate_index)
    creator = candidate["creator"]
    used_facts = load_used_facts()
    avoid_repeats = (
        "\n\nDo not repeat any of these previously used facts/hooks:\n"
        + "\n".join(f"- {fact}" for fact in used_facts)
        if used_facts
        else ""
    )
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
        f"{avoid_repeats}"
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

    # Count only the words TTS will actually speak — the metadata header
    # and [HOOK]/[BODY]/[CLOSE] tags aren't spoken, so counting them would
    # let a script pass this check while still running long (or short) once
    # synthesized.
    spoken_word_count = len(extract_spoken_text(text).split())
    if not (_MIN_WORDS <= spoken_word_count <= _MAX_WORDS):
        raise DraftError(
            f"script at {script_path} has {spoken_word_count} spoken words, "
            f"expected roughly {_MIN_WORDS}-{_MAX_WORDS} for a 20-30 second "
            "short-form video"
        )

    return text


def extract_spoken_text(script_text: str) -> str:
    """Strip everything in a script.md that isn't meant to be read aloud —
    the metadata header (title, clip source URL) above the '---' divider,
    and bare section tags like [HOOK]/[BODY]/[CLOSE] — leaving just the
    spoken commentary for TTS. Falls back to the full text if no divider
    is found, rather than guessing wrong and silently dropping content."""
    lines = script_text.splitlines()
    divider_index = next(
        (i for i, line in enumerate(lines) if line.strip() == _METADATA_DIVIDER), None
    )
    if divider_index is not None:
        lines = lines[divider_index + 1 :]

    spoken_lines = [line for line in lines if not _SECTION_TAG_RE.match(line.strip())]
    spoken_text = "\n".join(spoken_lines).strip()
    return spoken_text or script_text.strip()
