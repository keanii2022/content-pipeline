"""End-to-end vertical slice: one clip, one script, one output.

This is a CLI entrypoint, not an unattended pipeline. Discovery (Step 2)
and script drafting (Step 5) hand their actual work off to Claude Code
agents (`researcher`, `script-writer`, `content-reviewer`) that a plain
Python process cannot invoke itself, and both stages end at a human
checkpoint (picking a candidate, approving/editing a script) that must
stay manual. So `run_single.py` doesn't run the whole chain in one call —
it exposes one subcommand per stage, each printing what to do next, and
the human (or Claude, driving it interactively) runs them in order:

    discover -> record-candidates -> select-candidate -> fetch & voiceover -> assemble

Neither checkpoint is a formality here: `select-candidate` only creates
the script job and prints the script-writer prompt, and `voiceover`
re-validates the script via `pipeline.script.draft.load_script` before
synthesizing anything, so a stage genuinely cannot proceed past either
checkpoint until a human has actually chosen a candidate or approved a
script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pipeline.assemble.assemble import AssembleError, assemble_clip
from pipeline.discover.find_candidates import (
    DiscoveryError,
    build_research_prompt,
    find_candidates,
)
from pipeline.fetch.fetch_clip import FetchError, fetch_clip
from pipeline.script.draft import DraftError, start_script_job
from pipeline.voiceover.generate import VoiceoverError, generate_voiceover


def _cmd_discover(args: argparse.Namespace) -> None:
    prompt = build_research_prompt(args.creator_id)
    print(f"Run the `researcher` agent with this prompt for creator '{args.creator_id}':\n")
    print(prompt)
    print(
        "\nOnce you have its structured findings (a JSON list of "
        "{url, start_timestamp, end_timestamp, rationale} objects), save "
        "them to a file and run:\n"
        f"  record-candidates {args.creator_id} <candidates.json>"
    )


def _cmd_record_candidates(args: argparse.Namespace) -> None:
    with Path(args.candidates_file).open("r", encoding="utf-8") as f:
        candidates = json.load(f)

    out_path = find_candidates(args.creator_id, candidates, batch_id=args.batch_id)
    batch_id = out_path.stem
    print(f"Recorded {len(candidates)} candidate(s) to {out_path}\n")
    for index, candidate in enumerate(candidates):
        print(f"  [{index}] {candidate['url']} "
              f"({candidate['start_timestamp']}-{candidate['end_timestamp']})")
        print(f"      {candidate['rationale']}")
    print(
        "\nPick one candidate to proceed with, then run:\n"
        f"  select-candidate {args.creator_id} {batch_id} <index>"
    )


def _cmd_select_candidate(args: argparse.Namespace) -> None:
    job_id, work_dir, prompt = start_script_job(
        args.creator_id, args.batch_id, args.candidate_index, job_id=args.job_id
    )
    print(f"Created job '{job_id}' at {work_dir}\n")
    print(f"Run the `script-writer` agent with this prompt:\n")
    print(prompt)
    print(
        f"\nThen have the `content-reviewer` agent screen work/{job_id}/script.md "
        "for framing/tone, and review/edit it yourself. Once you approve the "
        "script, run:\n"
        f"  fetch {args.creator_id} {args.batch_id} {args.candidate_index}\n"
        f"  voiceover {job_id}"
    )


def _cmd_fetch(args: argparse.Namespace) -> None:
    out_dir = fetch_clip(
        args.creator_id,
        args.batch_id,
        args.candidate_index,
        clip_id=args.clip_id,
        yt_dlp_path=args.yt_dlp_path,
    )
    clip_id = out_dir.name
    print(f"Fetched clip to {out_dir}\n")
    print(
        "Once the voiceover for the matching job is also ready, run:\n"
        f"  assemble <job_id> {args.creator_id} {clip_id}"
    )


def _cmd_voiceover(args: argparse.Namespace) -> None:
    out_path = generate_voiceover(args.job_id)
    print(f"Generated voiceover at {out_path}\n")
    print(
        "Once the raw clip is also fetched, run:\n"
        f"  assemble {args.job_id} <creator_id> <clip_id>"
    )


def _cmd_assemble(args: argparse.Namespace) -> None:
    out_dir = assemble_clip(
        args.job_id, args.creator_id, args.clip_id, ffmpeg_path=args.ffmpeg_path
    )
    print(f"Assembled output at {out_dir / 'output.mp4'}")
    print(f"Manifest written to {out_dir / 'manifest.json'}")
    print(
        "\nThis pipeline stops here. Review the staged output yourself — "
        "nothing in this repo publishes or uploads it anywhere; posting it "
        "is a manual, external action."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one clip through discovery, scripting, fetch, "
        "voiceover, and assembly, one stage at a time."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_discover = subparsers.add_parser(
        "discover", help="Print the researcher-agent prompt for a permitted creator."
    )
    p_discover.add_argument("creator_id")
    p_discover.set_defaults(func=_cmd_discover)

    p_record = subparsers.add_parser(
        "record-candidates", help="Validate and persist a researcher agent's candidate findings."
    )
    p_record.add_argument("creator_id")
    p_record.add_argument("candidates_file", help="Path to a JSON file containing the candidate list.")
    p_record.add_argument("--batch-id", default=None)
    p_record.set_defaults(func=_cmd_record_candidates)

    p_select = subparsers.add_parser(
        "select-candidate", help="Start a script-drafting job for one chosen candidate."
    )
    p_select.add_argument("creator_id")
    p_select.add_argument("batch_id")
    p_select.add_argument("candidate_index", type=int)
    p_select.add_argument("--job-id", default=None)
    p_select.set_defaults(func=_cmd_select_candidate)

    p_fetch = subparsers.add_parser("fetch", help="Download the chosen candidate clip.")
    p_fetch.add_argument("creator_id")
    p_fetch.add_argument("batch_id")
    p_fetch.add_argument("candidate_index", type=int)
    p_fetch.add_argument("--clip-id", default=None)
    p_fetch.add_argument("--yt-dlp-path", default="yt-dlp")
    p_fetch.set_defaults(func=_cmd_fetch)

    p_voiceover = subparsers.add_parser(
        "voiceover", help="Synthesize the human-approved script into audio."
    )
    p_voiceover.add_argument("job_id")
    p_voiceover.set_defaults(func=_cmd_voiceover)

    p_assemble = subparsers.add_parser(
        "assemble", help="Combine the fetched clip and voiceover into staged output."
    )
    p_assemble.add_argument("job_id")
    p_assemble.add_argument("creator_id")
    p_assemble.add_argument("clip_id")
    p_assemble.add_argument("--ffmpeg-path", default="ffmpeg")
    p_assemble.set_defaults(func=_cmd_assemble)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (DiscoveryError, DraftError, FetchError, VoiceoverError, AssembleError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
