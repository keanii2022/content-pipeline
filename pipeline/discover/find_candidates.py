"""Candidate clip discovery for a single, already-permitted creator.

This module never fetches or downloads media. It scopes and records the
research question for a single creator (already on the permissions ledger),
and validates + persists the resulting candidate list — URL, timestamp
range, short rationale — to data/candidates/<creator_id>/<batch_id>.json for
a human to review and pick from or reject. No candidate is ever auto-selected.

The actual research (scanning a creator's recent public content) is
performed by the `researcher` agent, which is read-only and cannot write
files itself. This module supplies the scoped prompt for that agent
(`build_research_prompt`) and validates/persists whatever structured
candidate list the researcher's findings are turned into (`find_candidates`)
— it does not invoke the agent itself.
"""

from __future__ import annotations

import datetime
import json
import re
import secrets
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from pipeline.permissions import is_permitted, load_ledger

REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATES_DIR = REPO_ROOT / "data" / "candidates"

_REQUIRED_CANDIDATE_FIELDS = ("url", "start_timestamp", "end_timestamp", "rationale")
_ALL_KNOWN_CANDIDATE_FIELDS = set(_REQUIRED_CANDIDATE_FIELDS)

_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


class DiscoveryError(ValueError):
    """Raised when a creator lookup, permission check, or candidate is invalid."""


def get_creator(creator_id: str) -> dict[str, Any]:
    """Look up creator_id on the permissions ledger and confirm it is
    currently permitted. Raises DiscoveryError (fail loudly) if the creator
    is unknown or its permission has expired — discovery must never scan a
    creator that isn't cleared.
    """
    for entry in load_ledger():
        if entry["creator_id"] == creator_id:
            if not is_permitted(creator_id, entry["source_url"]):
                raise DiscoveryError(
                    f"creator '{creator_id}' is on the ledger but its permission has expired"
                )
            return entry
    raise DiscoveryError(f"creator '{creator_id}' is not on the permissions ledger")


def build_research_prompt(creator_id: str) -> str:
    """Build the scoped research prompt for the `researcher` agent, limited
    to this one permitted creator's public content."""
    creator = get_creator(creator_id)
    return (
        f"Scan {creator['display_name']}'s recent public content on "
        f"{creator['platform']} at {creator['source_url']} for clips worth "
        "using as source material for short-form commentary videos. "
        "Only consider content publicly posted by this creator at this URL. "
        "For each candidate, report: the clip's URL, a timestamp range "
        "(start/end), and a short rationale for why it could work. Do not "
        "rank or pick a single best candidate — surface options for a human "
        "to choose from."
    )


def _validate_candidate(candidate: dict[str, Any], index: int, creator: dict[str, Any]) -> None:
    if not isinstance(candidate, dict):
        raise DiscoveryError(f"candidates[{index}] must be a mapping")

    unknown = set(candidate) - _ALL_KNOWN_CANDIDATE_FIELDS
    if unknown:
        raise DiscoveryError(f"candidates[{index}] has unknown field(s): {sorted(unknown)}")

    for field in _REQUIRED_CANDIDATE_FIELDS:
        if field not in candidate:
            raise DiscoveryError(f"candidates[{index}] is missing required field '{field}'")
        if not isinstance(candidate[field], str) or not candidate[field].strip():
            raise DiscoveryError(f"candidates[{index}].{field} must be a non-empty string")

    candidate_host = urlparse(candidate["url"]).netloc.lower()
    creator_host = urlparse(creator["source_url"]).netloc.lower()
    if not candidate_host or candidate_host != creator_host:
        raise DiscoveryError(
            f"candidates[{index}].url '{candidate['url']}' is not on the permitted "
            f"creator's domain ({creator_host}) — refusing to persist an out-of-scope candidate"
        )


def _validate_path_component(value: str, label: str) -> None:
    if not _SAFE_PATH_COMPONENT.match(value):
        raise DiscoveryError(
            f"{label} '{value}' must contain only letters, digits, '.', '_', or '-'"
        )


def _generate_batch_id() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def find_candidates(
    creator_id: str,
    candidates: list[dict[str, Any]],
    *,
    batch_id: Optional[str] = None,
) -> Path:
    """Validate and persist a batch of candidate clips for creator_id.

    `candidates` is the structured result of the researcher agent's findings
    for this creator only — each item must have `url`, `start_timestamp`,
    `end_timestamp`, and `rationale`. Permission is re-checked here (not
    just when the prompt was built) since this is the point the output
    actually gets written to disk for later steps to read.
    """
    creator = get_creator(creator_id)
    _validate_path_component(creator_id, "creator_id")

    if not isinstance(candidates, list) or not candidates:
        raise DiscoveryError("candidates must be a non-empty list")
    for index, candidate in enumerate(candidates):
        _validate_candidate(candidate, index, creator)

    batch_id = batch_id or _generate_batch_id()
    _validate_path_component(batch_id, "batch_id")
    out_dir = CANDIDATES_DIR / creator_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{batch_id}.json"
    if out_path.exists():
        raise DiscoveryError(f"batch '{batch_id}' already exists for creator '{creator_id}'")

    payload = {
        "creator_id": creator_id,
        "source_url": creator["source_url"],
        "batch_id": batch_id,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "candidates": candidates,
    }
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    return out_path
