"""The content-reviewer agent's instructions must quote the same word
limit the code enforces.

This drifted once already: content-reviewer.md said "75-200 words" while
load_script() enforced 50-80, so the reviewer agent was judging scripts
against a length the pipeline would never accept. This test fails if the
two ever disagree again.
"""

import re
from pathlib import Path

from pipeline.script import draft

REVIEWER_INSTRUCTIONS = (
    Path(__file__).resolve().parents[1] / ".claude" / "agents" / "content-reviewer.md"
)
WORD_RANGE = re.compile(r"(\d+)\s*[-–]\s*(\d+)\s+(?:spoken\s+)?words")


def test_content_reviewer_word_limit_matches_code():
    text = REVIEWER_INSTRUCTIONS.read_text(encoding="utf-8")

    ranges = [(int(low), int(high)) for low, high in WORD_RANGE.findall(text)]

    assert ranges, "content-reviewer.md no longer states a word range"
    for word_range in ranges:
        assert word_range == (draft._MIN_WORDS, draft._MAX_WORDS)
