"""Loads and validates the creator-permission ledger.

The ledger (data/permissions/allowlist.yaml) is the single source of truth
for which creators have explicitly permitted clipping/reuse of their
content. The shape enforced here matches
data/permissions/allowlist.schema.json.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = REPO_ROOT / "data" / "permissions" / "allowlist.yaml"

_VALID_PLATFORMS = {"youtube", "tiktok", "instagram", "twitch", "other"}

_REQUIRED_STRING_FIELDS = (
    "creator_id",
    "display_name",
    "platform",
    "source_url",
    "permission_scope",
    "evidence",
)

_OPTIONAL_STRING_FIELDS = ("conditions", "notes")

_REQUIRED_DATE_FIELDS = ("permission_granted_date",)
_OPTIONAL_DATE_FIELDS = ("expiry_date",)

_ALL_KNOWN_FIELDS = (
    set(_REQUIRED_STRING_FIELDS)
    | set(_OPTIONAL_STRING_FIELDS)
    | set(_REQUIRED_DATE_FIELDS)
    | set(_OPTIONAL_DATE_FIELDS)
)


class LedgerError(ValueError):
    """Raised when the permissions ledger is missing, malformed, or invalid."""


def _normalize_date(value: Any, field: str, index: int) -> str:
    """Accept either an ISO date string or a value YAML already parsed as a
    date/datetime (unquoted 'YYYY-MM-DD' scalars auto-parse), and return a
    canonical ISO 8601 date string."""
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise LedgerError(
                f"creators[{index}].{field} '{value}' is not an ISO 8601 date (YYYY-MM-DD)"
            ) from exc
    raise LedgerError(f"creators[{index}].{field} must be an ISO 8601 date (YYYY-MM-DD)")


def _validate_entry(entry: dict[str, Any], index: int) -> None:
    if not isinstance(entry, dict):
        raise LedgerError(f"creators[{index}] must be a mapping")

    unknown = set(entry) - _ALL_KNOWN_FIELDS
    if unknown:
        raise LedgerError(f"creators[{index}] has unknown field(s): {sorted(unknown)}")

    for field in _REQUIRED_STRING_FIELDS:
        if field not in entry:
            raise LedgerError(f"creators[{index}] is missing required field '{field}'")
        if not isinstance(entry[field], str) or not entry[field].strip():
            raise LedgerError(f"creators[{index}].{field} must be a non-empty string")

    for field in _OPTIONAL_STRING_FIELDS:
        if field in entry and not isinstance(entry[field], str):
            raise LedgerError(f"creators[{index}].{field} must be a string")

    if entry["platform"] not in _VALID_PLATFORMS:
        raise LedgerError(
            f"creators[{index}].platform '{entry['platform']}' is not one of "
            f"{sorted(_VALID_PLATFORMS)}"
        )

    for field in _REQUIRED_DATE_FIELDS:
        if field not in entry:
            raise LedgerError(f"creators[{index}] is missing required field '{field}'")
        entry[field] = _normalize_date(entry[field], field, index)

    for field in _OPTIONAL_DATE_FIELDS:
        if entry.get(field) is not None:
            entry[field] = _normalize_date(entry[field], field, index)


def load_ledger(path: Path = ALLOWLIST_PATH) -> list[dict[str, Any]]:
    """Load and validate the permissions ledger, returning its creator entries."""
    if not path.exists():
        raise LedgerError(f"permissions ledger not found at {path}")

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise LedgerError(f"{path} must contain a top-level mapping")
    if set(data) - {"creators"}:
        raise LedgerError(f"{path} must only contain a top-level 'creators' key")
    if "creators" not in data:
        raise LedgerError(f"{path} is missing a top-level 'creators' list")

    creators = data["creators"]
    if not isinstance(creators, list):
        raise LedgerError(f"{path}: 'creators' must be a list")

    seen_ids: set[str] = set()
    for index, entry in enumerate(creators):
        _validate_entry(entry, index)
        creator_id = entry["creator_id"]
        if creator_id in seen_ids:
            raise LedgerError(f"duplicate creator_id '{creator_id}' at creators[{index}]")
        seen_ids.add(creator_id)

    return creators


def _is_expired(entry: dict[str, Any], as_of: datetime.date) -> bool:
    expiry = entry.get("expiry_date")
    if not expiry:
        return False
    return datetime.date.fromisoformat(expiry) < as_of


def is_permitted(
    creator_id: str,
    source_url: str,
    *,
    as_of: Optional[datetime.date] = None,
) -> bool:
    """Return True iff creator_id is on the ledger, its source_url matches, and it isn't expired.

    Raises LedgerError (rather than returning False) if the ledger itself is
    missing or malformed — a fail-closed choice: an unreadable ledger should
    stop the pipeline loudly, not be silently treated as "nothing permitted."
    """
    as_of = as_of or datetime.date.today()
    for entry in load_ledger():
        if entry["creator_id"] != creator_id:
            continue
        if entry["source_url"] != source_url:
            continue
        return not _is_expired(entry, as_of)
    return False
