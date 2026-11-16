"""Clip-mode cutting: turn recorded highlights into staged 9:16 clips.

Each selected highlight from work/clip-<source_id>/highlights.json is cut
out of the source with ffmpeg, keeping the source's own audio, and fitted
into 1080x1920 the same way commentary-mode assembly does it (scale to
fit, pad the rest; nothing is cropped). Every clip becomes its own job at
staged/<job_id>/ with output.mp4 and a manifest.json that carries the
source's provenance block, unchanged, and the highlight it came from. The
existing format validator then records its result in that manifest.

Nothing here publishes. A staged clip waits in the finished list for a
person to decide what is worth posting.
"""

from __future__ import annotations

import argparse
import datetime
import json
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from pipeline.assemble.assemble import _OUTPUT_HEIGHT, _OUTPUT_WIDTH
from pipeline.clip import captions as captions_module
from pipeline.clip.caption_styles import DEFAULT_STYLE, style_names
from pipeline.clip.captions import CaptionError
from pipeline.clip.highlights import HighlightsError, load_highlights
from pipeline.clip.intake import (
    RIGHTS_ALLOWLIST,
    RIGHTS_UNVERIFIED,
    IntakeError,
    load_source,
    provenance,
)
from pipeline.clip.transcribe import TranscribeError, load_transcript
from pipeline.format import validate as format_validate
from pipeline.format.profiles import PROFILES
from pipeline.permissions.validate import LedgerError, is_permitted

DEFAULT_PROFILE = "shorts"
MODE = "clip"
OUTPUT_FILE = "output.mp4"
SCALE_PAD_FILTER = (
    f"scale={_OUTPUT_WIDTH}:{_OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
    f"pad={_OUTPUT_WIDTH}:{_OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1"
)
_STAGED_HIGHLIGHT_FIELDS = (
    "rank",
    "start",
    "end",
    "duration_seconds",
    "hook_title",
    "score",
    "rationale",
)


class CutError(ValueError):
    """Raised when highlights can't be cut or a cut clip can't be staged."""


def _staged_dir() -> Path:
    # The validator's own directory, so a clip is always staged where
    # validate_format will look for it.
    return format_validate.STAGED_DIR


def _new_job_id() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def _check_permission_still_holds(source: dict[str, Any]) -> None:
    """A clip is only staged as `allowlist` if the ledger still says so today.

    Intake recorded the basis when the source was taken in; the ledger can
    change after that (an entry removed, edited, or expired). Staging the
    old answer would label a clip as permitted when it no longer is, so
    this refuses instead — and never rewrites the basis itself.
    """
    if source["rights_basis"] != RIGHTS_ALLOWLIST:
        return
    reference = source["permission_ledger_reference"]
    creator_id = reference["creator_id"]
    try:
        permitted = is_permitted(creator_id, reference.get("source_url"))
    except LedgerError as exc:
        raise CutError(f"could not re-check the permissions ledger: {exc}") from exc
    if not permitted:
        raise CutError(
            f"this source was taken in as rights_basis 'allowlist' for "
            f"'{creator_id}', but the ledger no longer permits it (entry "
            "removed, changed or expired). Take the source in again so its "
            "rights_basis is recorded as things stand now."
        )


def _select(highlights: list[dict[str, Any]], indices: Optional[Sequence[int]]) -> list[dict[str, Any]]:
    by_rank = {highlight["rank"]: highlight for highlight in highlights}
    if indices is None:
        return [by_rank[rank] for rank in sorted(by_rank)]

    if isinstance(indices, (str, bytes)) or not isinstance(indices, Sequence) or not indices:
        raise CutError("indices must be a non-empty list of highlight numbers")
    for index in indices:
        if isinstance(index, bool) or not isinstance(index, int):
            raise CutError(f"highlight number {index!r} is not a whole number")
    if len(set(indices)) != len(indices):
        raise CutError(f"highlight numbers repeat: {list(indices)}")
    unknown = sorted(set(indices) - set(by_rank))
    if unknown:
        raise CutError(
            f"no highlight numbered {unknown} — this source has {sorted(by_rank)}"
        )
    return [by_rank[rank] for rank in sorted(indices)]


def staged_clips(source_id: str) -> list[tuple[str, dict[str, Any]]]:
    """(job_id, manifest) for every clip already staged from this source, oldest first."""
    staged = _staged_dir()
    found: list[tuple[str, dict[str, Any]]] = []
    if not staged.is_dir():
        return found
    for manifest_path in sorted(staged.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # someone else's broken job shouldn't block cutting
        if not isinstance(manifest, dict):
            continue
        if manifest.get("mode") != MODE or manifest.get("source_id") != source_id:
            continue
        highlight = manifest.get("highlight")
        # Only a clip with real start/end times can be matched to a highlight.
        if isinstance(highlight, dict) and all(
            isinstance(highlight.get(key), (int, float)) and not isinstance(highlight.get(key), bool)
            for key in ("start", "end")
        ):
            found.append((manifest_path.parent.name, manifest))
    return found


def _cut_key(start: Any, end: Any, caption_style: Optional[str]) -> tuple[Any, Any, Optional[str]]:
    # A plain cut and each caption style of the same highlight are different
    # clips, so trying a look never requires removing another.
    return (start, end, caption_style)


def _caption_style_of(manifest: dict[str, Any]) -> Optional[str]:
    if "captions" not in manifest:
        return None
    recorded = manifest["captions"]
    style = recorded.get("style") if isinstance(recorded, dict) else None
    # A captioned clip whose style can't be read still isn't a plain clip.
    return style if isinstance(style, str) else "unknown"


def _already_cut(source_id: str) -> dict[tuple[Any, Any, Optional[str]], str]:
    """Map (start, end, caption style or None) -> job_id for every clip already staged from this source."""
    return {
        _cut_key(
            manifest["highlight"].get("start"),
            manifest["highlight"].get("end"),
            _caption_style_of(manifest),
        ): job_id
        for job_id, manifest in staged_clips(source_id)
    }


def _run_ffmpeg(
    source_path: Path,
    start: float,
    length: float,
    output_path: Path,
    ffmpeg_path: str,
    captions_file: Optional[Path] = None,
    captions_top: int = 0,
) -> None:
    scale_pad = SCALE_PAD_FILTER
    cmd = [
        ffmpeg_path,
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        # Seeking on the input and re-encoding lands on the exact frame;
        # a stream copy could only cut at keyframes, seconds off the word.
        "-ss", f"{start:.3f}",
        "-t", f"{length:.3f}",
        "-i", str(source_path),
    ]
    if captions_file is None:
        cmd += [
            # V, not v: skips cover art, which would make a still-image "clip".
            "-map", "0:V:0",
            "-map", "0:a:0",
            "-map_chapters", "-1",
            "-vf", scale_pad,
        ]
    else:
        # -ss/-t above apply to the source only; the caption timeline
        # already starts at 0 = clip start.
        cmd += [
            "-f", "concat",
            "-safe", "0",
            "-i", str(captions_file),
            "-filter_complex",
            f"[0:V:0]{scale_pad}[base];"
            "[1:v]format=rgba,fps=30[captions];"
            f"[base][captions]overlay=0:{captions_top}:eof_action=pass[v]",
            "-map", "[v]",
            "-map", "0:a:0",
            "-map_chapters", "-1",
        ]
    cmd += [
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-f", "mp4",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise CutError(f"'{ffmpeg_path}' not found — is ffmpeg installed?") from exc
    except subprocess.CalledProcessError as exc:
        detail = "\n".join((exc.stderr or "").strip().splitlines()[-8:])
        raise CutError(f"ffmpeg failed (exit {exc.returncode}): {detail}") from exc
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise CutError("ffmpeg finished but wrote no video")


def _stage_clip(
    source_id: str,
    source_path: Path,
    source: dict[str, Any],
    highlight: dict[str, Any],
    work_dir: Path,
    ffmpeg_path: str,
    lines: Optional[list[dict[str, Any]]] = None,
    caption_style: str = DEFAULT_STYLE,
) -> str:
    """Cut one highlight and move it into staged/ as a finished job.

    The clip is built in the source's work folder and only moved into
    staged/ once the video and its manifest both exist, so the finished
    list never shows a half-written job.
    """
    job_id = _new_job_id()
    staged = _staged_dir()
    job_dir = staged / job_id
    if job_dir.exists():
        raise CutError(f"staged output already exists: {job_dir}")

    build_dir = work_dir / f"cut-{job_id}.partial"
    build_dir.mkdir()
    length = round(highlight["end"] - highlight["start"], 3)
    captions_dir = build_dir / "captions"
    try:
        captions_file = None
        if lines is not None:
            try:
                captions_file = captions_module.render_captions(
                    lines, captions_dir, length, caption_style
                )
            except (CaptionError, OSError) as exc:
                raise CutError(f"could not draw the captions: {exc}") from exc
        _run_ffmpeg(
            source_path,
            highlight["start"],
            length,
            build_dir / OUTPUT_FILE,
            ffmpeg_path,
            captions_file,
            captions_module.style_for(caption_style).strip_top,
        )
        shutil.rmtree(captions_dir, ignore_errors=True)
        manifest = {
            "job_id": job_id,
            "mode": MODE,
            "output_file": OUTPUT_FILE,
            "source_id": source_id,
            "source": source,
            "highlight": {field: highlight[field] for field in _STAGED_HIGHLIGHT_FIELDS},
            "audio": "source",
            **(
                {"captions": {"style": caption_style, "lines": len(lines)}}
                if lines is not None
                else {}
            ),
            "assembled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        (build_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        staged.mkdir(parents=True, exist_ok=True)
        if job_dir.exists():
            raise CutError(f"staged output already exists: {job_dir}")
        build_dir.rename(job_dir)
    except BaseException:
        shutil.rmtree(build_dir, ignore_errors=True)
        raise
    return job_id


def cut_highlights(
    source_id: str,
    indices: Optional[Sequence[int]] = None,
    profile: str = DEFAULT_PROFILE,
    *,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    captions: bool = False,
    caption_style: str = DEFAULT_STYLE,
) -> dict[str, Any]:
    """Cut a source's highlights into staged clips and format-check each.

    `indices` are highlight numbers (their rank in highlights.json, from
    1). With none given, every highlight not already cut is cut; naming
    one that is already staged is refused rather than cut twice. "Already
    cut" is per highlight and caption style (or plain).

    With `captions`, word-by-word captions from the source's transcript
    are burned in, in `caption_style` (a name from caption_styles). No
    transcript, or no words in a highlight, is refused rather than cut
    without captions. A non-default style without `captions` is refused
    rather than ignored.

    Returns {"source_id", "profile", "rights_basis", "staged": [...],
    "already_cut": [...]}. Raises CutError before cutting anything if the
    source, its highlights, the selection or the profile is not usable.
    """
    if profile not in PROFILES:
        raise CutError(f"unknown format profile '{profile}' — choices are {sorted(PROFILES)}")
    if not isinstance(caption_style, str) or caption_style not in style_names():
        raise CutError(f"unknown caption style {caption_style!r} — choices are {style_names()}")
    if captions is False and caption_style != DEFAULT_STYLE:
        raise CutError(f"caption style '{caption_style}' was given but captions are off")
    try:
        source_path, metadata = load_source(source_id)
        work_dir, record = load_highlights(source_id)
    except (IntakeError, HighlightsError) as exc:
        raise CutError(str(exc)) from exc

    source = provenance(metadata)
    if record["source"] != source:
        raise CutError(
            "highlights.json records different provenance than the source's "
            "own metadata — record the highlights again"
        )
    _check_permission_still_holds(source)

    if captions is not True and captions is not False:
        raise CutError("captions must be True or False")
    if captions:
        try:
            captions_module.require_pillow()
        except CaptionError as exc:
            raise CutError(str(exc)) from exc
        try:
            _, transcript = load_transcript(source_id)
        except TranscribeError as exc:
            raise CutError(f"captions need the transcript: {exc}") from exc
        if transcript.get("source") != source:
            raise CutError(
                "transcript.json records different provenance than the source's "
                "own metadata — transcribe it again"
            )

    selected = _select(record["highlights"], indices)
    existing = _already_cut(source_id)

    def key(highlight: dict[str, Any]) -> tuple[Any, Any, Optional[str]]:
        return _cut_key(highlight["start"], highlight["end"], caption_style if captions else None)

    already_cut = [
        {"rank": highlight["rank"], "job_id": existing[key(highlight)]}
        for highlight in selected
        if key(highlight) in existing
    ]
    if already_cut and indices is not None:
        listed = ", ".join(f"#{item['rank']} (staged/{item['job_id']})" for item in already_cut)
        raise CutError(f"already cut: {listed} — remove that staged job first to cut it again")
    done_ranks = {item["rank"] for item in already_cut}
    to_cut = [highlight for highlight in selected if highlight["rank"] not in done_ranks]
    if not to_cut:
        raise CutError(f"every highlight of '{source_id}' is already cut and staged")

    lines_by_rank: dict[int, Optional[list[dict[str, Any]]]] = {}
    for highlight in to_cut:
        lines_by_rank[highlight["rank"]] = None
        if captions:
            lines = captions_module.caption_lines(
                transcript, highlight["start"], highlight["end"], caption_style
            )
            if not lines:
                raise CutError(
                    f"highlight #{highlight['rank']} has no transcribed words to caption "
                    f"between {highlight['start']:g}s and {highlight['end']:g}s"
                )
            lines_by_rank[highlight["rank"]] = lines

    staged: list[dict[str, Any]] = []

    def staged_so_far() -> str:
        if not staged:
            return ""
        return " (staged before this: " + ", ".join(item["job_id"] for item in staged) + ")"

    for highlight in to_cut:
        try:
            job_id = _stage_clip(
                source_id,
                source_path,
                source,
                highlight,
                work_dir,
                ffmpeg_path,
                lines_by_rank[highlight["rank"]],
                caption_style,
            )
        except CutError as exc:
            raise CutError(f"highlight #{highlight['rank']}: {exc}{staged_so_far()}") from exc
        try:
            compliance = format_validate.validate_format(
                job_id, profile, ffprobe_path=ffprobe_path
            )
        except format_validate.FormatError as exc:
            raise CutError(
                f"highlight #{highlight['rank']} is staged as {job_id}, but its "
                f"format check could not run: {exc}{staged_so_far()}"
            ) from exc
        staged.append(
            {
                "job_id": job_id,
                "rank": highlight["rank"],
                "start": highlight["start"],
                "end": highlight["end"],
                "hook_title": highlight["hook_title"],
                "score": highlight["score"],
                "format_compliance": compliance,
            }
        )

    return {
        "source_id": source_id,
        "profile": profile,
        "rights_basis": source["rights_basis"],
        "staged": staged,
        "already_cut": already_cut,
    }


def _highlight_numbers(value: str) -> list[int]:
    try:
        return [int(part) for part in value.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{value}' should be highlight numbers separated by commas, like 1,3,5"
        ) from None


def add_caption_arguments(parser: argparse.ArgumentParser) -> None:
    """--captions and --caption-style, shared by both cut commands."""
    parser.add_argument(
        "--captions",
        action="store_true",
        help="burn in word-by-word captions from the transcript (default: off)",
    )
    parser.add_argument(
        "--caption-style",
        choices=style_names(),
        help=f"caption look, with --captions (default: {DEFAULT_STYLE})",
    )


def caption_style_from(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """The style to cut with; a style without --captions is a usage error, not a no-op."""
    if args.caption_style is not None and not args.captions:
        parser.error("--caption-style needs --captions")
    return args.caption_style or DEFAULT_STYLE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.clip.cut",
        description="Cut a source's recorded highlights into staged 9:16 clips.",
    )
    parser.add_argument("source_id")
    parser.add_argument(
        "--only",
        type=_highlight_numbers,
        metavar="1,3,5",
        help="cut just these highlight numbers (default: all not yet cut)",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE, choices=sorted(PROFILES))
    add_caption_arguments(parser)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    caption_style = caption_style_from(parser, args)

    try:
        result = cut_highlights(
            args.source_id,
            args.only,
            args.profile,
            captions=args.captions,
            caption_style=caption_style,
        )
    except CutError as exc:
        print(f"cut failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"Cut {len(result['staged'])} clip(s) from {result['source_id']} "
        f"(rights_basis: {result['rights_basis']}):"
    )
    for clip in result["staged"]:
        compliance = clip["format_compliance"]
        verdict = "pass" if compliance["passed"] else "FAIL — " + "; ".join(compliance["reasons"])
        print(f"  #{clip['rank']} [score {clip['score']}] {clip['hook_title']}")
        print(f"     {clip['start']:g}-{clip['end']:g}s  ->  staged/{clip['job_id']}/{OUTPUT_FILE}")
        print(f"     format ({result['profile']}): {verdict}")
    for item in result["already_cut"]:
        print(f"  #{item['rank']} was already cut — left alone (staged/{item['job_id']})")
    if result["rights_basis"] == RIGHTS_UNVERIFIED:
        print(
            "\nrights_basis is 'unverified': nobody has checked that this source "
            "may be reused. That call is yours before anything is posted."
        )
    print(
        "\nNothing is published. The clips are in the control panel's finished "
        "list for you to review."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
