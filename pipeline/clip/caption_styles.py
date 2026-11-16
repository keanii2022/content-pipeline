"""The named caption looks clip mode can burn in.

This table is the one list of styles: the cut commands' --caption-style
choices, the control API and the dashboard all read it, so a look added
or renamed here shows up everywhere at once (a test checks they agree).
Every look uses the one bundled font; only sizes, colours, case, how the
spoken word is marked, line length and position differ.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

MARK_COLOR = "color"
MARK_BOX = "box"
CASE_UPPER = "upper"
CASE_AS_SPOKEN = "as-spoken"

RGBA = tuple[int, int, int, int]


@dataclass(frozen=True)
class CaptionStyle:
    name: str
    description: str
    font_size: int
    min_font_size: int
    outline_width: int
    text_color: RGBA
    outline_color: RGBA
    mark: str
    lit_text_color: RGBA
    box_color: Optional[RGBA]
    box_padding: int
    box_radius: int
    case: str
    max_words_per_line: int
    max_line_chars: int
    strip_top: int
    strip_height: int
    max_text_width: int

    def cased(self, text: str) -> str:
        return text.upper() if self.case == CASE_UPPER else text


_BLACK = (0, 0, 0, 255)
_WHITE = (255, 255, 255, 255)

STYLES: dict[str, CaptionStyle] = {
    style.name: style
    for style in (
        CaptionStyle(
            name="bold-white-yellow-word",
            description="Bold white caps, dark outline, spoken word in yellow, lower third.",
            font_size=84,
            min_font_size=40,
            outline_width=7,
            text_color=_WHITE,
            outline_color=_BLACK,
            mark=MARK_COLOR,
            lit_text_color=(255, 214, 0, 255),
            box_color=None,
            box_padding=0,
            box_radius=0,
            case=CASE_UPPER,
            max_words_per_line=3,
            max_line_chars=16,
            strip_top=1240,
            strip_height=300,
            max_text_width=960,
        ),
        CaptionStyle(
            name="bold-white-purple-box",
            description="Bold white caps, spoken word on a purple box, lower third.",
            font_size=80,
            min_font_size=40,
            outline_width=6,
            text_color=_WHITE,
            outline_color=_BLACK,
            mark=MARK_BOX,
            lit_text_color=_WHITE,
            box_color=(124, 58, 237, 255),
            box_padding=16,
            box_radius=18,
            case=CASE_UPPER,
            max_words_per_line=3,
            max_line_chars=16,
            strip_top=1240,
            strip_height=300,
            max_text_width=920,
        ),
        CaptionStyle(
            name="clean-sentence-cyan-word",
            description="Smaller sentence-case text, up to four words, spoken word in cyan.",
            font_size=66,
            min_font_size=36,
            outline_width=5,
            text_color=_WHITE,
            outline_color=_BLACK,
            mark=MARK_COLOR,
            lit_text_color=(0, 229, 255, 255),
            box_color=None,
            box_padding=0,
            box_radius=0,
            case=CASE_AS_SPOKEN,
            max_words_per_line=4,
            max_line_chars=24,
            strip_top=1280,
            strip_height=240,
            max_text_width=980,
        ),
    )
}

DEFAULT_STYLE = "bold-white-yellow-word"


def style_names() -> list[str]:
    """Every style name, default first, in the order they should be offered."""
    return list(STYLES)


def get_style(name: str) -> CaptionStyle:
    """The style called `name`; KeyError (with the choices) if there is none."""
    try:
        return STYLES[name]
    except (KeyError, TypeError):
        raise KeyError(f"unknown caption style {name!r} — choices are {style_names()}") from None
