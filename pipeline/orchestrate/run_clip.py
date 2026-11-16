"""Clip mode from the command line: one long video in, staged short clips out.

Like run_single.py, this is one subcommand per stage rather than one
unattended run, and each prints the exact command for the next stage:

    intake | intake-file -> transcribe -> find-highlights ->
    record-highlights -> cut -> status

It can't be a single call: picking the highlights is handed to the
`highlight-finder` agent in a live Claude Code session, which a plain
Python process can't start. `find-highlights` prints that agent's prompt
and `record-highlights` takes its answer back in.

The chain ends at `cut`, with clips in staged/ for a person to review.
There is no publish subcommand here and nothing in this file uploads:
posting a staged clip is `run_single publish` (or the control panel's
button), a separate step a person takes per clip. This file only says so.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

from pipeline.clip.cut import (
    DEFAULT_PROFILE,
    OUTPUT_FILE,
    CutError,
    _highlight_numbers,
    cut_highlights,
)
from pipeline.clip.highlights import (
    AGENT_NAME,
    HighlightsError,
    _clock,
    build_prompt,
    record_highlights,
)
from pipeline.clip.intake import (
    RIGHTS_UNVERIFIED,
    IntakeError,
    intake_file,
    intake_url,
)
from pipeline.clip.status import describe_source
from pipeline.clip.transcribe import MODEL_SIZES, TranscribeError, transcribe
from pipeline.format.profiles import PROFILES

_UNVERIFIED_NOTE = (
    "rights_basis is 'unverified': nobody has checked that this source may "
    "be reused. That call is yours before anything is posted."
)


def _python() -> str:
    """The interpreter to show in a next command, so it can be pasted as is."""
    try:
        shown = str(Path(sys.executable).relative_to(Path.cwd()))
    except ValueError:
        # Not a bare "python": on a Mac that name often doesn't exist.
        shown = sys.executable
    return shlex.quote(shown)


def _command(*parts: str) -> str:
    words = " ".join(part if part.startswith("<") else shlex.quote(part) for part in parts)
    return f"  {_python()} -m pipeline.orchestrate.run_clip {words}"


def _publish_note(rights_basis: Any) -> str:
    """How a staged clip gets posted — by the person, never by this command."""
    note = (
        "Posting a clip is a separate step you take yourself, one clip at a "
        "time: the control panel's Publish button, or\n"
        f'  {_python()} -m pipeline.orchestrate.run_single publish <job_id> "<title>"'
    )
    if rights_basis == RIGHTS_UNVERIFIED:
        note += (
            "\nThis source is 'unverified', so that upload is refused unless "
            "you tick the rights box in the control panel or add "
            "--acknowledge-unverified-rights — only once you've confirmed you "
            "have the right to post the clip."
        )
    return note


def _span(highlight: dict[str, Any]) -> str:
    return f"{_clock(highlight['start'])}-{_clock(highlight['end'])}"


def _format_verdict(compliance: Any) -> str:
    if not isinstance(compliance, dict):
        return "format: not checked yet"
    if compliance.get("passed") is True:
        return f"format ({compliance.get('profile')}): pass"
    reasons = compliance.get("reasons")
    reasons = "; ".join(str(reason) for reason in reasons) if isinstance(reasons, list) else ""
    return f"format ({compliance.get('profile')}): FAIL — {reasons}"


def _report_intake(source_dir: Path) -> None:
    metadata = json.loads((source_dir / "metadata.json").read_text(encoding="utf-8"))
    source_id = metadata["source_id"]
    print(f"Took in source '{source_id}' -> {source_dir}")
    print(f"  title:        {metadata['title']}")
    print(f"  duration:     {metadata['duration_seconds'] / 60:.1f} min")
    print(f"  rights_basis: {metadata['rights_basis']}")
    if metadata["rights_basis"] == RIGHTS_UNVERIFIED:
        print(f"\n{_UNVERIFIED_NOTE}")
    print(
        "\nNext, transcribe it (this runs on your machine and takes a while "
        "for a long video):\n" + _command("transcribe", source_id)
    )


def _cmd_intake(args: argparse.Namespace) -> None:
    _report_intake(intake_url(args.url))


def _cmd_intake_file(args: argparse.Namespace) -> None:
    _report_intake(intake_file(args.path, own=args.own))


def _cmd_transcribe(args: argparse.Namespace) -> None:
    progress_shown = False

    def show_progress(fraction: float) -> None:
        nonlocal progress_shown
        progress_shown = True
        print(f"\rtranscribing… {fraction:4.0%}", end="", file=sys.stderr, flush=True)

    try:
        work_dir = transcribe(args.source_id, model_size=args.model, on_progress=show_progress)
    finally:
        if progress_shown:
            print(file=sys.stderr)  # end the progress line before anything else prints

    transcript = json.loads((work_dir / "transcript.json").read_text(encoding="utf-8"))
    print(f"Transcribed source '{args.source_id}' -> {work_dir / 'transcript.txt'}")
    print(f"  language: {transcript['language']}")
    print(f"  segments: {len(transcript['segments'])}")
    print("\nNext, get the highlight-finder's prompt:\n" + _command("find-highlights", args.source_id))


def _cmd_find_highlights(args: argparse.Namespace) -> None:
    prompt = build_prompt(args.source_id)
    print(f"Run the `{AGENT_NAME}` agent with this prompt:\n")
    print(prompt)
    print(
        "\nSave the JSON list it returns to a file, then run:\n"
        + _command("record-highlights", args.source_id, "<picks.json>")
    )


def _cmd_record_highlights(args: argparse.Namespace) -> None:
    out_path = record_highlights(args.source_id, args.picks_file)
    record = json.loads(out_path.read_text(encoding="utf-8"))
    print(f"Recorded {len(record['highlights'])} highlight(s) to {out_path}\n")
    for highlight in record["highlights"]:
        print(
            f"  #{highlight['rank']:<2} score {highlight['score']:<3} {_span(highlight)} "
            f"({highlight['duration_seconds']:.0f}s)  {highlight['hook_title']}"
        )
    print("\nCut them all into clips:\n" + _command("cut", args.source_id))
    count = len(record["highlights"])
    if count > 1:
        # An example that would run as pasted: only numbers that exist.
        example = "1,3" if count >= 3 else "1"
        print(
            "or only some, by their number:\n"
            + _command("cut", args.source_id, "--only", example)
        )


def _cmd_cut(args: argparse.Namespace) -> None:
    result = cut_highlights(args.source_id, args.only, args.profile)
    print(
        f"Cut {len(result['staged'])} clip(s) from source '{result['source_id']}' "
        f"(rights_basis: {result['rights_basis']}):"
    )
    for clip in result["staged"]:
        print(f"  #{clip['rank']:<2} score {clip['score']:<3} {_span(clip)}  {clip['hook_title']}")
        print(f"      staged/{clip['job_id']}/{OUTPUT_FILE}  {_format_verdict(clip['format_compliance'])}")
    for item in result["already_cut"]:
        print(f"  #{item['rank']:<2} was already cut — left alone (staged/{item['job_id']})")
    if result["rights_basis"] == RIGHTS_UNVERIFIED:
        print(f"\n{_UNVERIFIED_NOTE}")
    print(
        "\nClip mode stops here. Nothing is published: the clips are in the "
        "control panel's finished list for you to review.\n"
        + _publish_note(result["rights_basis"])
        + "\nTo see where this source stands at any time:\n"
        + _command("status", args.source_id)
    )


def _cmd_status(args: argparse.Namespace) -> None:
    source = describe_source(args.source_id)
    source_id = source["source_id"]
    duration = source["duration_seconds"]
    print(f"Source '{source_id}'")
    print(f"  title:        {source['title']}")
    print(f"  from:         {source['origin']}")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        print(f"  duration:     {duration / 60:.1f} min")
    print(f"  rights_basis: {source['rights_basis']}")
    print("\n  [x] taken in")

    if not source["transcribed"]:
        print("  [ ] transcribed")
    else:
        print(f"  [x] transcribed ({source['language']}, {source['segments']} segments)")
        if not source["highlights_recorded"]:
            print("  [ ] highlights recorded")
        else:
            highlights = source["highlights"]
            print(f"  [x] highlights recorded ({len(highlights)})")
            mark = "x" if source["clips_cut"] == len(highlights) else " "
            print(f"  [{mark}] clips cut ({source['clips_cut']} of {len(highlights)})\n")
            for highlight in highlights:
                print(
                    f"  #{highlight['rank']:<2} score {highlight['score']:<3} {_span(highlight)}  "
                    f"{highlight['hook_title']}"
                )
                if highlight["job_id"] is None:
                    print("      not cut yet")
                else:
                    print(
                        f"      staged/{highlight['job_id']}/{OUTPUT_FILE}  "
                        f"{_format_verdict(highlight['format_compliance'])}"
                    )

    if source["other_staged_clips"]:
        print(
            f"\n  {source['other_staged_clips']} more staged clip(s) from this source "
            "came from earlier highlight picks."
        )
    if source["rights_basis"] == RIGHTS_UNVERIFIED:
        print(f"\n{_UNVERIFIED_NOTE}")

    if source["next"] is None:
        print(
            "\nNothing left to run. The clips are in the control panel's "
            "finished list for you to review; nothing is published.\n"
            + _publish_note(source["rights_basis"])
        )
    else:
        print("\nNext:\n" + _command(source["next"], source_id))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.orchestrate.run_clip",
        description="Clip mode: turn one long video into staged short clips, one stage at a time.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_intake = subparsers.add_parser("intake", help="Download one video from a URL.")
    p_intake.add_argument("url")
    p_intake.set_defaults(func=_cmd_intake)

    p_intake_file = subparsers.add_parser("intake-file", help="Take in a video file from this machine.")
    p_intake_file.add_argument("path")
    p_intake_file.add_argument(
        "--own",
        action="store_true",
        help="this is your own footage (recorded as rights_basis 'own')",
    )
    p_intake_file.set_defaults(func=_cmd_intake_file)

    p_transcribe = subparsers.add_parser("transcribe", help="Transcribe a source, word by word.")
    p_transcribe.add_argument("source_id")
    p_transcribe.add_argument("--model", choices=MODEL_SIZES, help="Whisper model size")
    p_transcribe.set_defaults(func=_cmd_transcribe)

    p_find = subparsers.add_parser(
        "find-highlights", help=f"Print the prompt for the {AGENT_NAME} agent."
    )
    p_find.add_argument("source_id")
    p_find.set_defaults(func=_cmd_find_highlights)

    p_record = subparsers.add_parser(
        "record-highlights", help=f"Check and record the {AGENT_NAME} agent's picks."
    )
    p_record.add_argument("source_id")
    p_record.add_argument("picks_file", help="file holding the agent's JSON list")
    p_record.set_defaults(func=_cmd_record_highlights)

    p_cut = subparsers.add_parser("cut", help="Cut recorded highlights into staged 9:16 clips.")
    p_cut.add_argument("source_id")
    p_cut.add_argument(
        "--only",
        type=_highlight_numbers,
        metavar="1,3,5",
        help="cut just these highlight numbers (default: all not yet cut)",
    )
    p_cut.add_argument("--profile", default=DEFAULT_PROFILE, choices=sorted(PROFILES))
    p_cut.set_defaults(func=_cmd_cut)

    p_status = subparsers.add_parser("status", help="Show how far a source has got and what's next.")
    p_status.add_argument("source_id")
    p_status.set_defaults(func=_cmd_status)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (IntakeError, TranscribeError, HighlightsError, CutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
