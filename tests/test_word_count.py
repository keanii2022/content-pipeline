"""load_script() only accepts scripts with 50-80 spoken words.

That range is what pipeline/script/draft.py enforces (_MIN_WORDS = 50,
_MAX_WORDS = 80): a 20-30 second short. Only the words read aloud count —
the header above '---' and the [HOOK]/[BODY]/[CLOSE] tags don't.
"""

import pytest

from pipeline.script import draft


def write_script(work_dir, job_id, spoken_words):
    job_dir = work_dir / job_id
    job_dir.mkdir()
    body = " ".join(["word"] * spoken_words)
    (job_dir / "script.md").write_text(
        "SCRIPT — Test title (NASA, 2026)\n"
        "Clip source: YouTube, https://example.com/clip (0:00–0:30)\n"
        "\n"
        "---\n"
        "\n"
        "[HOOK]\n"
        "\n"
        "[BODY]\n"
        f"{body}\n"
        "\n"
        "[CLOSE]\n"
    )


@pytest.fixture
def work_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(draft, "WORK_DIR", tmp_path)
    return tmp_path


@pytest.mark.parametrize("spoken_words", [50, 80])
def test_word_count_accepts_scripts_at_its_limits(work_dir, spoken_words):
    write_script(work_dir, "job-1", spoken_words)

    text = draft.load_script("job-1")

    assert len(draft.extract_spoken_text(text).split()) == spoken_words


@pytest.mark.parametrize("spoken_words", [49, 81])
def test_word_count_rejects_scripts_just_outside_its_limits(work_dir, spoken_words):
    write_script(work_dir, "job-1", spoken_words)

    with pytest.raises(draft.DraftError, match=f"has {spoken_words} spoken words"):
        draft.load_script("job-1")
