"""Burned-in, word-by-word captions for clip-mode cuts.

This machine's ffmpeg has no `ass`, `subtitles` or `drawtext` filter, so
captions can't be handed to ffmpeg as text. Instead Pillow draws every
caption state (one line with one word lit) as a transparent PNG, and an
ffconcat list says how long each PNG stays up. cut.py layers that list on
top of the clip with ffmpeg's `overlay`, which every ffmpeg build has.

One look only: bold white uppercase text with a dark outline, the word
being spoken in yellow, in the lower third of the 1080x1920 frame.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

from pipeline.assemble.assemble import _OUTPUT_WIDTH

CAPTION_STYLE = "bold-white-yellow-word"
FONT_PATH = Path(__file__).resolve().parent / "fonts" / "Montserrat-Bold.ttf"

MAX_WORDS_PER_LINE = 3
MAX_LINE_CHARS = 16
PAUSE_SECONDS = 0.6
LINGER_SECONDS = 0.3
SENTENCE_ENDINGS = (".", "?", "!", "…")

CAPTION_TOP = 1240
CAPTION_HEIGHT = 300
FONT_SIZE = 84
MIN_FONT_SIZE = 40
OUTLINE_WIDTH = 7
MAX_TEXT_WIDTH = 960
TEXT_COLOR = (255, 255, 255, 255)
LIT_COLOR = (255, 214, 0, 255)
OUTLINE_COLOR = (0, 0, 0, 255)

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


def _starts_new_line(line: list[dict[str, Any]], word: dict[str, Any]) -> bool:
    previous = line[-1]
    return (
        len(line) >= MAX_WORDS_PER_LINE
        or word["start"] - previous["end"] >= PAUSE_SECONDS
        or previous["text"].endswith(SENTENCE_ENDINGS)
        or len(" ".join(item["text"] for item in line + [word])) > MAX_LINE_CHARS
    )


def caption_lines(transcript: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    """Group the highlight's words into caption lines, in clip time.

    Each line is {"start", "end", "words": [{"text", "start", "end"}]}.
    A word's start/end is when it is lit (from its own start until the
    next word in the line starts); a line's end is when it clears. Every
    time is in [0, clip length].
    """
    length = round(end - start, 3)
    groups: list[list[dict[str, Any]]] = []
    for word in _words_in_range(transcript, start, end):
        if groups and not _starts_new_line(groups[-1], word):
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


def _draw_line(words: list[str], lit_index: int, path: Path) -> None:
    Image, ImageDraw, _ = _pillow()
    image = Image.new("RGBA", (_OUTPUT_WIDTH, CAPTION_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    size = FONT_SIZE
    font = _font(size)
    width = font.getlength(" ".join(words)) + 2 * OUTLINE_WIDTH
    if width > MAX_TEXT_WIDTH:
        size = max(MIN_FONT_SIZE, int(size * MAX_TEXT_WIDTH / width))
        font = _font(size)
        width = font.getlength(" ".join(words)) + 2 * OUTLINE_WIDTH

    space = font.getlength(" ")
    x = (_OUTPUT_WIDTH - width) / 2 + OUTLINE_WIDTH
    for index, word in enumerate(words):
        draw.text(
            (x, CAPTION_HEIGHT / 2),
            word,
            font=font,
            anchor="lm",
            fill=LIT_COLOR if index == lit_index else TEXT_COLOR,
            stroke_width=OUTLINE_WIDTH,
            stroke_fill=OUTLINE_COLOR,
        )
        x += font.getlength(word) + space
    image.save(path)


def _milliseconds(seconds: float) -> int:
    return int(round(seconds * 1000))


def render_captions(lines: list[dict[str, Any]], build_dir: Path, length: float) -> Path:
    """Draw every caption state into build_dir and write the ffconcat timing file.

    One PNG per (line, lit word) plus a blank one for the gaps; the
    durations in the timing file add up to exactly `length` seconds.
    Returns the timing file's path.
    """
    Image, _, _ = _pillow()
    build_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (_OUTPUT_WIDTH, CAPTION_HEIGHT), (0, 0, 0, 0)).save(build_dir / BLANK_PNG)

    total = _milliseconds(length)
    states: list[tuple[str, int, int]] = []
    cursor = 0
    for line_number, line in enumerate(lines, start=1):
        texts = [word["text"].upper() for word in line["words"]]
        line_start = min(max(_milliseconds(line["start"]), cursor), total)
        if line_start > cursor:
            states.append((BLANK_PNG, cursor, line_start))
        for word_number, word in enumerate(line["words"], start=1):
            name = f"line{line_number:03d}-word{word_number}.png"
            _draw_line(texts, word_number - 1, build_dir / name)
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
