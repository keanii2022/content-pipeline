"""Batch/queue generalization of run_single.py's stage-by-stage flow.

This runs the same discover -> record-candidates -> select-candidate ->
fetch & voiceover -> assemble -> check-format chain as run_single.py, but
over several creators at once instead of exactly one. It does not change
that chain's shape or its human checkpoints: discovery and script drafting
still hand off to Claude Code agents this process cannot invoke, and both
still end at a human checkpoint (picking a candidate, approving/editing a
script) that stays manual per clip, exactly as in run_single.py.

What this module adds is bookkeeping: a "run" is a queue of creator_ids
(by default, every creator currently on the permissions ledger; optionally
a subset listed in a queue file) tracked in
data/jobs/<run_id>/state.json, so each per-creator stage command can be
invoked by run_id + creator_id alone — no need to re-type batch_id,
candidate_index, job_id, or clip_id by hand across a multi-day, multi-clip
run the way run_single.py's single-job commands require.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml

from pipeline.assemble.assemble import AssembleError, assemble_clip
from pipeline.discover.find_candidates import (
    DiscoveryError,
    build_research_prompt,
    find_candidates,
)
from pipeline.fetch.fetch_clip import FetchError, fetch_clip
from pipeline.format.profiles import PROFILES
from pipeline.format.validate import FormatError, validate_format
from pipeline.permissions import LedgerError, load_ledger
from pipeline.script.draft import DraftError, start_script_job
from pipeline.voiceover.generate import VoiceoverError, generate_voiceover

REPO_ROOT = Path(__file__).resolve().parents[2]
JOBS_DIR = REPO_ROOT / "data" / "jobs"

_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


class BatchError(ValueError):
    """Raised when a run, queue file, or queue entry is invalid."""


def _validate_path_component(value: str, label: str) -> None:
    if not _SAFE_PATH_COMPONENT.match(value):
        raise BatchError(
            f"{label} '{value}' must contain only letters, digits, '.', '_', or '-'"
        )


def _generate_run_id() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _run_dir(run_id: str) -> Path:
    _validate_path_component(run_id, "run_id")
    return JOBS_DIR / run_id


def _state_path(run_id: str) -> Path:
    return _run_dir(run_id) / "state.json"


def _load_state(run_id: str) -> dict[str, Any]:
    state_path = _state_path(run_id)
    if not state_path.exists():
        raise BatchError(f"no run found at {state_path}")
    with state_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_state_atomic(state: dict[str, Any]) -> None:
    """Write state.json via write-temp-then-rename so a crash mid-write
    can't leave a corrupted file; the temp file lives in the same directory
    so the rename is on one filesystem and therefore atomic."""
    state_path = _state_path(state["run_id"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=state_path.parent, prefix=".state.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, state_path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_name)
        raise


@contextlib.contextmanager
def _run_lock(run_id: str):
    """Serialize load-mutate-save cycles for one run across processes,
    using a lock file separate from state.json itself — state.json is
    replaced via atomic rename on every save, and flock-ing a path whose
    underlying inode can be swapped out from under it does not actually
    serialize anything; a dedicated, never-replaced lock file does."""
    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / ".state.lock"
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


@contextlib.contextmanager
def _editable_state(run_id: str):
    """Load a run's state under an exclusive lock, hand it to the caller to
    mutate (and to run whatever pipeline call the mutation depends on), and
    persist it atomically — but only if the caller's block completes
    without raising, so a failed fetch/voiceover/assemble/etc. call leaves
    the entry's recorded state unchanged, matching run_single.py's
    only-record-on-success convention."""
    with _run_lock(run_id):
        state = _load_state(run_id)
        yield state
        _write_state_atomic(state)


def _get_entry(state: dict[str, Any], creator_id: str) -> dict[str, Any]:
    for entry in state["entries"]:
        if entry["creator_id"] == creator_id:
            return entry
    raise BatchError(f"creator '{creator_id}' is not in run '{state['run_id']}'")


def _new_entry(creator_id: str) -> dict[str, Any]:
    return {
        "creator_id": creator_id,
        "batch_id": None,
        "candidate_index": None,
        "job_id": None,
        "clip_id": None,
        "voiceover_generated": False,
        "assembled": False,
        "format_profile": None,
        "format_passed": None,
        "updated_at": _now(),
    }


_DOWNSTREAM_OF_SELECTION = (
    "clip_id",
    "voiceover_generated",
    "assembled",
    "format_profile",
    "format_passed",
)
_DOWNSTREAM_OF_RECORDING = ("candidate_index", "job_id") + _DOWNSTREAM_OF_SELECTION


def _reset_fields(entry: dict[str, Any], fields: tuple[str, ...]) -> None:
    """Reset the given fields to their fresh-entry defaults. Used whenever
    an earlier stage is re-run (new candidates recorded, a different
    candidate selected) so results tied to the now-superseded batch_id or
    job_id can't linger and make _describe_entry report a stale "done"."""
    defaults = _new_entry(entry["creator_id"])
    for field in fields:
        entry[field] = defaults[field]


def _describe_entry(entry: dict[str, Any]) -> str:
    """Human-readable stage + next action for one queue entry, derived from
    its recorded fields rather than a separately tracked status string, so
    the description can never drift out of sync with what's actually
    happened."""
    creator_id = entry["creator_id"]
    if entry["format_passed"] is not None:
        result = "PASSED" if entry["format_passed"] else "FAILED"
        return (
            f"[{creator_id}] format-checked against '{entry['format_profile']}': "
            f"{result} (done)"
        )
    if entry["assembled"]:
        return f"[{creator_id}] assembled — next: check-format <run_id> {creator_id} <profile>"
    if entry["job_id"] and entry["clip_id"] and entry["voiceover_generated"]:
        return f"[{creator_id}] fetched + voiceover ready — next: assemble <run_id> {creator_id}"
    if entry["job_id"]:
        missing = []
        if not entry["clip_id"]:
            missing.append(f"fetch <run_id> {creator_id}")
        if not entry["voiceover_generated"]:
            missing.append(f"voiceover <run_id> {creator_id}")
        return f"[{creator_id}] script job '{entry['job_id']}' created — next: {', '.join(missing)}"
    if entry["batch_id"] is not None:
        return (
            f"[{creator_id}] candidates recorded in batch '{entry['batch_id']}' — "
            f"next: select-candidate <run_id> {creator_id} <candidate_index>"
        )
    return f"[{creator_id}] pending discovery — next: discover <run_id> {creator_id}"


def _resolve_creator_ids(queue_file: Optional[str]) -> list[str]:
    if queue_file is None:
        try:
            ledger = load_ledger()
        except LedgerError as exc:
            raise BatchError(str(exc)) from exc
        creator_ids = [entry["creator_id"] for entry in ledger]
    else:
        queue_path = Path(queue_file)
        if not queue_path.exists():
            raise BatchError(f"queue file not found at {queue_path}")
        with queue_path.open("r", encoding="utf-8") as f:
            queue = yaml.safe_load(f)
        if queue is None:
            queue = {}
        if not isinstance(queue, dict):
            raise BatchError(f"{queue_path} must be a YAML mapping with a 'creator_ids' list")
        raw_ids = queue.get("creator_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise BatchError(f"{queue_path} must define a non-empty 'creator_ids' list")
        creator_ids = raw_ids

    if not creator_ids:
        raise BatchError("no creators to queue — permissions ledger is empty")
    for creator_id in creator_ids:
        if not isinstance(creator_id, str):
            raise BatchError(f"creator_id {creator_id!r} must be a string")
        _validate_path_component(creator_id, "creator_id")
    if len(set(creator_ids)) != len(creator_ids):
        raise BatchError("queue has duplicate creator_ids — one clip per creator per run")
    return creator_ids


def _cmd_start_run(args: argparse.Namespace) -> None:
    creator_ids = _resolve_creator_ids(args.queue_file)
    run_id = args.run_id or _generate_run_id()
    _validate_path_component(run_id, "run_id")

    state_path = _state_path(run_id)
    if state_path.exists():
        raise BatchError(f"run '{run_id}' already exists at {state_path}")

    state = {
        "run_id": run_id,
        "queue_source": args.queue_file or "permissions ledger",
        "created_at": _now(),
        "entries": [_new_entry(creator_id) for creator_id in creator_ids],
    }
    with _run_lock(run_id):
        if state_path.exists():
            raise BatchError(f"run '{run_id}' already exists at {state_path}")
        _write_state_atomic(state)

    print(f"Started run '{run_id}' with {len(creator_ids)} creator(s):\n")
    for entry in state["entries"]:
        print(f"  {_describe_entry(entry)}")
    print(f"\nCheck progress any time with:\n  status {run_id}")


def _cmd_status(args: argparse.Namespace) -> None:
    state = _load_state(args.run_id)
    print(f"Run '{args.run_id}' ({state['queue_source']}, started {state['created_at']}):\n")
    for entry in state["entries"]:
        print(f"  {_describe_entry(entry)}")


def _cmd_discover(args: argparse.Namespace) -> None:
    state = _load_state(args.run_id)
    _get_entry(state, args.creator_id)  # confirms creator_id is in this run
    prompt = build_research_prompt(args.creator_id)
    print(f"Run the `researcher` agent with this prompt for creator '{args.creator_id}':\n")
    print(prompt)
    print(
        "\nOnce you have its structured findings (a JSON list of "
        "{url, start_timestamp, end_timestamp, rationale} objects), save "
        "them to a file and run:\n"
        f"  record-candidates {args.run_id} {args.creator_id} <candidates.json>"
    )


def _cmd_record_candidates(args: argparse.Namespace) -> None:
    with Path(args.candidates_file).open("r", encoding="utf-8") as f:
        candidates = json.load(f)

    with _editable_state(args.run_id) as state:
        entry = _get_entry(state, args.creator_id)
        out_path = find_candidates(args.creator_id, candidates, batch_id=args.batch_id)
        batch_id = out_path.stem
        # Re-running discovery for this creator supersedes whatever candidate
        # was previously selected — clear it and everything downstream so
        # status can't keep reporting a stale job/fetch/assemble/format result.
        _reset_fields(entry, _DOWNSTREAM_OF_RECORDING)
        entry["batch_id"] = batch_id
        entry["updated_at"] = _now()

    print(f"Recorded {len(candidates)} candidate(s) to {out_path}\n")
    for index, candidate in enumerate(candidates):
        print(f"  [{index}] {candidate['url']} "
              f"({candidate['start_timestamp']}-{candidate['end_timestamp']})")
        print(f"      {candidate['rationale']}")
    print(
        "\nPick one candidate to proceed with, then run:\n"
        f"  select-candidate {args.run_id} {args.creator_id} <index>"
    )


def _cmd_select_candidate(args: argparse.Namespace) -> None:
    with _editable_state(args.run_id) as state:
        entry = _get_entry(state, args.creator_id)
        if entry["batch_id"] is None:
            raise BatchError(
                f"no candidates recorded yet for '{args.creator_id}' in run '{args.run_id}' "
                "— run record-candidates first"
            )

        job_id, work_dir, prompt = start_script_job(
            args.creator_id, entry["batch_id"], args.candidate_index, job_id=args.job_id
        )
        # Selecting a (possibly different) candidate supersedes any previous
        # job for this creator — clear fetch/voiceover/assemble/format state
        # tied to that old job_id before recording the new one.
        _reset_fields(entry, _DOWNSTREAM_OF_SELECTION)
        entry["candidate_index"] = args.candidate_index
        entry["job_id"] = job_id
        entry["updated_at"] = _now()

    print(f"Created job '{job_id}' at {work_dir}\n")
    print("Run the `script-writer` agent with this prompt:\n")
    print(prompt)
    print(
        f"\nThen have the `content-reviewer` agent screen work/{job_id}/script.md "
        "for framing/tone, and review/edit it yourself. Once you approve the "
        "script, run:\n"
        f"  fetch {args.run_id} {args.creator_id}\n"
        f"  voiceover {args.run_id} {args.creator_id}"
    )


def _cmd_fetch(args: argparse.Namespace) -> None:
    with _editable_state(args.run_id) as state:
        entry = _get_entry(state, args.creator_id)
        if entry["job_id"] is None:
            raise BatchError(
                f"no candidate selected yet for '{args.creator_id}' in run '{args.run_id}' "
                "— run select-candidate first"
            )

        out_dir = fetch_clip(
            args.creator_id,
            entry["batch_id"],
            entry["candidate_index"],
            clip_id=args.clip_id,
            yt_dlp_path=args.yt_dlp_path,
        )
        entry["clip_id"] = out_dir.name
        entry["updated_at"] = _now()

    print(f"Fetched clip to {out_dir}\n")
    print(
        "Once the voiceover for this creator's job is also ready, run:\n"
        f"  assemble {args.run_id} {args.creator_id}"
    )


def _cmd_voiceover(args: argparse.Namespace) -> None:
    with _editable_state(args.run_id) as state:
        entry = _get_entry(state, args.creator_id)
        if entry["job_id"] is None:
            raise BatchError(
                f"no candidate selected yet for '{args.creator_id}' in run '{args.run_id}' "
                "— run select-candidate first"
            )

        out_path = generate_voiceover(entry["job_id"])
        entry["voiceover_generated"] = True
        entry["updated_at"] = _now()

    print(f"Generated voiceover at {out_path}\n")
    print(
        "Once the raw clip is also fetched, run:\n"
        f"  assemble {args.run_id} {args.creator_id}"
    )


def _cmd_assemble(args: argparse.Namespace) -> None:
    with _editable_state(args.run_id) as state:
        entry = _get_entry(state, args.creator_id)
        if not entry["clip_id"] or not entry["voiceover_generated"]:
            raise BatchError(
                f"'{args.creator_id}' in run '{args.run_id}' needs both a fetched clip "
                "and a generated voiceover before it can be assembled"
            )

        out_dir = assemble_clip(
            entry["job_id"], args.creator_id, entry["clip_id"], ffmpeg_path=args.ffmpeg_path
        )
        entry["assembled"] = True
        entry["updated_at"] = _now()

    print(f"Assembled output at {out_dir / 'output.mp4'}")
    print(f"Manifest written to {out_dir / 'manifest.json'}")
    print(
        "\nOnce ready, check it against a platform's format profile:\n"
        f"  check-format {args.run_id} {args.creator_id} <profile>\n"
        f"  (profiles: {', '.join(sorted(PROFILES))})"
    )


def _cmd_check_format(args: argparse.Namespace) -> None:
    with _editable_state(args.run_id) as state:
        entry = _get_entry(state, args.creator_id)
        if not entry["assembled"]:
            raise BatchError(
                f"'{args.creator_id}' in run '{args.run_id}' has not been assembled yet"
            )

        result = validate_format(entry["job_id"], args.profile, ffprobe_path=args.ffprobe_path)
        entry["format_profile"] = args.profile
        entry["format_passed"] = result["passed"]
        entry["updated_at"] = _now()
        job_id = entry["job_id"]

    status = "PASSED" if result["passed"] else "FAILED"
    print(
        f"Format compliance for '{args.creator_id}' (job '{job_id}') against "
        f"profile '{args.profile}': {status}"
    )
    for reason in result["reasons"]:
        print(f"  - {reason}")
    print(f"\nResult recorded in staged/{job_id}/manifest.json")
    print(
        "\nThis pipeline stops here. Review the staged output yourself — "
        "nothing uploads it automatically. Posting it is a manual action: "
        "the `publish` command or the control panel's YouTube button, or "
        "by hand for any other platform."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run several creators through discovery, scripting, fetch, "
        "voiceover, and assembly, one stage at a time, tracked as a single run."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_start = subparsers.add_parser(
        "start-run", help="Start a new run from the permissions ledger or a queue file."
    )
    p_start.add_argument(
        "--queue-file",
        default=None,
        help="Path to a YAML file with a 'creator_ids' list. Defaults to every "
        "creator on the permissions ledger.",
    )
    p_start.add_argument("--run-id", default=None)
    p_start.set_defaults(func=_cmd_start_run)

    p_status = subparsers.add_parser("status", help="Show progress for every creator in a run.")
    p_status.add_argument("run_id")
    p_status.set_defaults(func=_cmd_status)

    p_discover = subparsers.add_parser(
        "discover", help="Print the researcher-agent prompt for one creator in a run."
    )
    p_discover.add_argument("run_id")
    p_discover.add_argument("creator_id")
    p_discover.set_defaults(func=_cmd_discover)

    p_record = subparsers.add_parser(
        "record-candidates", help="Validate and persist a researcher agent's candidate findings."
    )
    p_record.add_argument("run_id")
    p_record.add_argument("creator_id")
    p_record.add_argument("candidates_file", help="Path to a JSON file containing the candidate list.")
    p_record.add_argument("--batch-id", default=None)
    p_record.set_defaults(func=_cmd_record_candidates)

    p_select = subparsers.add_parser(
        "select-candidate", help="Start a script-drafting job for one chosen candidate."
    )
    p_select.add_argument("run_id")
    p_select.add_argument("creator_id")
    p_select.add_argument("candidate_index", type=int)
    p_select.add_argument("--job-id", default=None)
    p_select.set_defaults(func=_cmd_select_candidate)

    p_fetch = subparsers.add_parser("fetch", help="Download the chosen candidate clip.")
    p_fetch.add_argument("run_id")
    p_fetch.add_argument("creator_id")
    p_fetch.add_argument("--clip-id", default=None)
    p_fetch.add_argument("--yt-dlp-path", default="yt-dlp")
    p_fetch.set_defaults(func=_cmd_fetch)

    p_voiceover = subparsers.add_parser(
        "voiceover", help="Synthesize the human-approved script into audio."
    )
    p_voiceover.add_argument("run_id")
    p_voiceover.add_argument("creator_id")
    p_voiceover.set_defaults(func=_cmd_voiceover)

    p_assemble = subparsers.add_parser(
        "assemble", help="Combine the fetched clip and voiceover into staged output."
    )
    p_assemble.add_argument("run_id")
    p_assemble.add_argument("creator_id")
    p_assemble.add_argument("--ffmpeg-path", default="ffmpeg")
    p_assemble.set_defaults(func=_cmd_assemble)

    p_check_format = subparsers.add_parser(
        "check-format", help="Validate staged output against a platform format profile."
    )
    p_check_format.add_argument("run_id")
    p_check_format.add_argument("creator_id")
    p_check_format.add_argument("profile", choices=sorted(PROFILES))
    p_check_format.add_argument("--ffprobe-path", default="ffprobe")
    p_check_format.set_defaults(func=_cmd_check_format)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (
        BatchError,
        DiscoveryError,
        DraftError,
        FetchError,
        VoiceoverError,
        AssembleError,
        FormatError,
        LedgerError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
