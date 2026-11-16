"""Burned-in, word-by-word captions for clip-mode cuts.

This machine's ffmpeg has no `ass`, `subtitles` or `drawtext` filter, so
captions can't be handed to ffmpeg as text. Instead Pillow draws every
caption state (one line with one word lit) as a transparent PNG, and an
ffconcat list says how long each PNG stays up. cut.py layers that list on
top of the clip with ffmpeg's `overlay`, which every ffmpeg build has.

The looks themselves (size, colours, how the spoken word is marked,
case, words per line, position) live in caption_styles.py. `preview`
draws each look over a real frame of a highlight so they can be compared
without cutting anything.
"""

from __future__ import annotations

import argparse
import functools
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from pipeline.assemble.assemble import _OUTPUT_HEIGHT, _OUTPUT_WIDTH
from pipeline.clip.caption_styles import DEFAULT_STYLE, MARK_BOX, CaptionStyle, get_style, style_names
from pipeline.clip.highlights import HighlightsError, load_highlights
from pipeline.clip.intake import IntakeError, load_source, provenance
from pipeline.clip.transcribe import TranscribeError, load_transcript

FONT_PATH = Path(__file__).resolve().parent / "fonts" / "Montserrat-Bold.ttf"

PAUSE_SECONDS = 0.6
LINGER_SECONDS = 0.3
SENTENCE_ENDINGS = (".", "?", "!", "…")

BLANK_PNG = "blank.png"
TIMING_FILE = "captions.ffconcat"


class CaptionError(ValueError):
    """Raised when captions can't be built for a clip."""


def _pillow() -> tuple[Any, Any, Any]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise CaptionError(
            'captions need Pillow, which isn\'t installed: .venv/bin/pip install -e ".[captions]"'
        ) from exc
    return Image, ImageDraw, ImageFont


def require_pillow() -> None:
    """Fail early, before anything is cut, if captions can't be drawn here."""
    _pillow()


def style_for(name: str) -> CaptionStyle:
    """The named style, or CaptionError listing the choices."""
    try:
        return get_style(name)
    except KeyError as exc:
        raise CaptionError(exc.args[0]) from None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _words_in_range(transcript: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    length = round(end - start, 3)
    words = []
    for segment in transcript.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for word in segment.get("words") or []:
            if not isinstance(word, dict):
                continue
            text = word.get("word")
            word_start, word_end = word.get("start"), word.get("end")
            if not isinstance(text, str) or not text.strip():
                continue
            if not _is_number(word_start) or not _is_number(word_end):
                continue
            if not start <= (word_start + word_end) / 2 < end:
                continue
            clip_start = round(min(max(word_start - start, 0.0), length), 3)
            clip_end = round(min(max(word_end - start, 0.0), length), 3)
            words.append({"text": text.strip(), "start": clip_start, "end": max(clip_end, clip_start)})
    words.sort(key=lambda word: (word["start"], word["end"]))
    return words


def _starts_new_line(line: list[dict[str, Any]], word: dict[str, Any], style: CaptionStyle) -> bool:
    previous = line[-1]
    return (
        len(line) >= style.max_words_per_line
        or word["start"] - previous["end"] >= PAUSE_SECONDS
        or previous["text"].endswith(SENTENCE_ENDINGS)
        or len(" ".join(item["text"] for item in line + [word])) > style.max_line_chars
    )


def caption_lines(
    transcript: dict[str, Any], start: float, end: float, style: str = DEFAULT_STYLE
) -> list[dict[str, Any]]:
    """Group the highlight's words into caption lines, in clip time.

    How many words fit on a line comes from the named style.

    Each line is {"start", "end", "words": [{"text", "start", "end"}]}.
    A word's start/end is when it is lit (from its own start until the
    next word in the line starts); a line's end is when it clears. Every
    time is in [0, clip length].
    """
    look = style_for(style)
    length = round(end - start, 3)
    groups: list[list[dict[str, Any]]] = []
    for word in _words_in_range(transcript, start, end):
        if groups and not _starts_new_line(groups[-1], word, look):
            groups[-1].append(word)
        else:
            groups.append([word])

    lines = []
    for index, group in enumerate(groups):
        clears = min(group[-1]["end"] + LINGER_SECONDS, length)
        if index + 1 < len(groups):
            clears = min(clears, groups[index + 1][0]["start"])
        clears = round(clears, 3)
        lit = []
        for position, word in enumerate(group):
            lit_until = group[position + 1]["start"] if position + 1 < len(group) else clears
            lit.append({"text": word["text"], "start": word["start"], "end": lit_until})
        lines.append({"start": group[0]["start"], "end": clears, "words": lit})
    return lines


@functools.lru_cache(maxsize=None)
def _font(size: int) -> Any:
    _, _, ImageFont = _pillow()
    return ImageFont.truetype(str(FONT_PATH), size)


def _line_image(words: list[str], lit_index: int, style: CaptionStyle) -> Any:
    """One caption state: the line with word `lit_index` marked, on a transparent strip."""
    Image, ImageDraw, _ = _pillow()
    image = Image.new("RGBA", (_OUTPUT_WIDTH, style.strip_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    words = [style.cased(word) for word in words]
    boxed = style.mark == MARK_BOX
    # A boxed word needs its padding inside the width budget too.
    margin = style.outline_width + (style.box_padding if boxed else 0)
    # ...and extra room between words so a box never touches its neighbours.
    extra_gap = style.box_padding if boxed else 0

    def line_width(font: Any) -> float:
        return font.getlength(" ".join(words)) + extra_gap * (len(words) - 1) + 2 * margin

    size = style.font_size
    font = _font(size)
    width = line_width(font)
    if width > style.max_text_width:
        size = max(style.min_font_size, int(size * style.max_text_width / width))
        font = _font(size)
        width = line_width(font)

    space = font.getlength(" ") + extra_gap
    middle = style.strip_height / 2
    x = (_OUTPUT_WIDTH - width) / 2 + margin
    for index, word in enumerate(words):
        lit = index == lit_index
        if lit and boxed:
            left, top, right, bottom = draw.textbbox((x, middle), word, font=font, anchor="lm")
            pad = style.box_padding
            draw.rounded_rectangle(
                (left - pad, top - pad, right + pad, bottom + pad),
                radius=style.box_radius,
                fill=style.box_color,
            )
            draw.text((x, middle), word, font=font, anchor="lm", fill=style.lit_text_color)
        else:
            draw.text(
                (x, middle),
                word,
                font=font,
                anchor="lm",
                fill=style.lit_text_color if lit else style.text_color,
                stroke_width=style.outline_width,
                stroke_fill=style.outline_color,
            )
        x += font.getlength(word) + space
    return image


def _milliseconds(seconds: float) -> int:
    return int(round(seconds * 1000))


def render_captions(
    lines: list[dict[str, Any]], build_dir: Path, length: float, style: str = DEFAULT_STYLE
) -> Path:
    """Draw every caption state into build_dir, in the named style, and write the ffconcat timing file.

    One PNG per (line, lit word) plus a blank one for the gaps; the
    durations in the timing file add up to exactly `length` seconds.
    Returns the timing file's path.
    """
    Image, _, _ = _pillow()
    look = style_for(style)
    build_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (_OUTPUT_WIDTH, look.strip_height), (0, 0, 0, 0)).save(build_dir / BLANK_PNG)

    total = _milliseconds(length)
    states: list[tuple[str, int, int]] = []
    cursor = 0
    for line_number, line in enumerate(lines, start=1):
        texts = [word["text"] for word in line["words"]]
        line_start = min(max(_milliseconds(line["start"]), cursor), total)
        if line_start > cursor:
            states.append((BLANK_PNG, cursor, line_start))
        for word_number, word in enumerate(line["words"], start=1):
            name = f"line{line_number:03d}-word{word_number}.png"
            _line_image(texts, word_number - 1, look).save(build_dir / name)
            word_start = min(max(_milliseconds(word["start"]), cursor), total)
            word_end = min(max(_milliseconds(word["end"]), word_start), total)
            states.append((name, word_start, word_end))
            cursor = word_end
    if cursor < total:
        states.append((BLANK_PNG, cursor, total))

    shown = [(name, begin, finish) for name, begin, finish in states if finish > begin]
    entries = ["ffconcat version 1.0"]
    for name, begin, finish in shown:
        entries += [f"file {name}", f"duration {(finish - begin) / 1000:.3f}"]
    # The concat demuxer ignores the last entry's duration unless the file
    # is listed once more after it.
    entries.append(f"file {shown[-1][0]}")
    timing = build_dir / TIMING_FILE
    timing.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return timing


def composite_preview(
    frame_path: Path, words: list[str], lit_index: int, style: str, output_path: Path
) -> Path:
    """Draw one caption state in the named style over a 1080x1920 frame and save it."""
    Image, _, _ = _pillow()
    look = style_for(style)
    with Image.open(frame_path) as frame:
        canvas = frame.convert("RGBA")
    if canvas.size != (_OUTPUT_WIDTH, _OUTPUT_HEIGHT):
        raise CaptionError(
            f"preview frame is {canvas.size[0]}x{canvas.size[1]}, not {_OUTPUT_WIDTH}x{_OUTPUT_HEIGHT}"
        )
    canvas.alpha_composite(_line_image(words, lit_index, look), (0, look.strip_top))
    canvas.convert("RGB").save(output_path)
    return output_path


def _grab_frame(source_path: Path, at: float, output_path: Path, ffmpeg_path: str) -> None:
    # Imported here because cut.py imports this module.
    from pipeline.clip.cut import SCALE_PAD_FILTER

    cmd = [
        ffmpeg_path,
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{at:.3f}",
        "-i", str(source_path),
        "-map", "0:V:0",
        "-frames:v", "1",
        "-update", "1",
        "-vf", SCALE_PAD_FILTER,
        "-y", str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise CaptionError(f"'{ffmpeg_path}' not found — is ffmpeg installed?") from exc
    except subprocess.CalledProcessError as exc:
        detail = "\n".join((exc.stderr or "").strip().splitlines()[-8:])
        raise CaptionError(f"ffmpeg could not grab a frame (exit {exc.returncode}): {detail}") from exc
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise CaptionError("ffmpeg finished but wrote no frame")


def _line_holding(lines: list[dict[str, Any]], word: dict[str, Any]) -> tuple[list[str], int]:
    for line in lines:
        for index, candidate in enumerate(line["words"]):
            if candidate["start"] == word["start"] and candidate["text"] == word["text"]:
                return [item["text"] for item in line["words"]], index
    raise CaptionError(f"the word {word['text']!r} is in no caption line")


def preview_path(work_dir: Path, rank: int, style: str) -> Path:
    return work_dir / f"caption-preview-{rank}-{style}.png"


def preview_styles(source_id: str, rank: int, *, ffmpeg_path: str = "ffmpeg") -> list[Path]:
    """Write one still per caption style over a real frame of highlight #rank.

    The frame is taken while a word of that highlight is being spoken, and
    each style shows the real line holding that word, with it lit. Stills
    go into the source's work folder; nothing is cut or staged.
    """
    if isinstance(rank, bool) or not isinstance(rank, int):
        raise CaptionError(f"highlight number {rank!r} is not a whole number")
    require_pillow()
    try:
        source_path, metadata = load_source(source_id)
        work_dir, record = load_highlights(source_id)
        _, transcript = load_transcript(source_id)
    except (IntakeError, HighlightsError, TranscribeError) as exc:
        raise CaptionError(str(exc)) from exc

    source = provenance(metadata)
    if record["source"] != source:
        raise CaptionError(
            "highlights.json records different provenance than the source's "
            "own metadata — record the highlights again"
        )
    if transcript.get("source") != source:
        raise CaptionError(
            "transcript.json records different provenance than the source's "
            "own metadata — transcribe it again"
        )

    by_rank = {highlight["rank"]: highlight for highlight in record["highlights"]}
    if rank not in by_rank:
        raise CaptionError(f"no highlight numbered {rank} — this source has {sorted(by_rank)}")
    highlight = by_rank[rank]
    words = _words_in_range(transcript, highlight["start"], highlight["end"])
    if not words:
        raise CaptionError(f"highlight #{rank} has no transcribed words to caption")
    word = words[len(words) // 2]
    moment = highlight["start"] + (word["start"] + word["end"]) / 2

    frame_path = work_dir / f"caption-preview-{rank}-frame.png"
    written = []
    try:
        _grab_frame(source_path, moment, frame_path, ffmpeg_path)
        for style in style_names():
            lines = caption_lines(transcript, highlight["start"], highlight["end"], style)
            texts, lit_index = _line_holding(lines, word)
            written.append(
                composite_preview(frame_path, texts, lit_index, style, preview_path(work_dir, rank, style))
            )
    finally:
        frame_path.unlink(missing_ok=True)
    return written


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.clip.captions",
        description="Caption tools for clip mode.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    p_preview = subparsers.add_parser(
        "preview",
        help="Draw every caption style over a real frame of one highlight, without cutting.",
    )
    p_preview.add_argument("source_id")
    p_preview.add_argument("rank", type=int, help="the highlight's number")
    args = parser.parse_args(argv)

    try:
        paths = preview_styles(args.source_id, args.rank)
    except CaptionError as exc:
        print(f"preview failed: {exc}", file=sys.stderr)
        return 1
    print(f"One still per caption style for highlight #{args.rank} of {args.source_id}:")
    for path in paths:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
