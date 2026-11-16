"""Caption style presets: the table, cutting in a chosen style, and preview stills.

Cutting uses the stand-in ffmpeg/ffprobe from test_clip_cut; the preview
frame grab is answered with a real 1080x1920 PNG so the stills are drawn
for real with Pillow. Nothing is published.
"""

import argparse
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from pipeline.clip import caption_styles, captions, cut
from pipeline.clip.caption_styles import (
    DEFAULT_STYLE,
    MARK_BOX,
    MARK_COLOR,
    STYLES,
    get_style,
    style_names,
)
from pipeline.clip.captions import CaptionError, caption_lines, composite_preview, preview_styles
from pipeline.clip.cut import CutError, cut_highlights
from pipeline.orchestrate import run_clip
from test_clip_cut import (  # noqa: F401
    SOURCE_ID,
    UNVERIFIED,
    Project,
    dirs,
    refused,
    tools,
)

BOX_STYLE = next(name for name, style in STYLES.items() if style.mark == MARK_BOX)


def transcript_of(*words):
    return {
        "segments": [
            {
                "start": 0.0,
                "end": 1000.0,
                "text": "",
                "words": [{"start": s, "end": e, "word": f" {text}"} for s, e, text in words],
            }
        ]
    }


def solid_frame(path, color=(40, 90, 160)):
    Image.new("RGB", (1080, 1920), color).save(path)
    return path


# --- the table ---------------------------------------------------------------


def test_there_are_exactly_three_styles_and_the_old_look_is_the_default():
    assert len(style_names()) == 3
    assert DEFAULT_STYLE == "bold-white-yellow-word"
    assert style_names()[0] == DEFAULT_STYLE


def test_the_default_style_is_the_step_23_look_unchanged():
    style = get_style(DEFAULT_STYLE)
    assert (style.font_size, style.min_font_size, style.outline_width) == (84, 40, 7)
    assert style.text_color == (255, 255, 255, 255)
    assert style.lit_text_color == (255, 214, 0, 255)
    assert style.mark == MARK_COLOR
    assert style.cased("hello") == "HELLO"
    assert (style.max_words_per_line, style.max_line_chars) == (3, 16)
    assert (style.strip_top, style.strip_height, style.max_text_width) == (1240, 300, 960)


def test_one_style_marks_the_word_with_a_box_and_one_keeps_the_spoken_case():
    assert get_style(BOX_STYLE).box_color is not None
    assert any(style.cased("Hello") == "Hello" for style in STYLES.values())


@pytest.mark.parametrize("name", style_names())
def test_every_style_fits_the_frame(name):
    style = get_style(name)
    assert 0 < style.strip_top and style.strip_top + style.strip_height <= 1920
    assert style.max_text_width <= 1080
    assert style.min_font_size <= style.font_size
    assert style.mark in (MARK_COLOR, MARK_BOX)
    assert (style.mark == MARK_BOX) == (style.box_color is not None)


def test_an_unknown_style_is_a_clear_error():
    with pytest.raises(KeyError, match="choices"):
        get_style("comic-sans-rainbow")
    with pytest.raises(CaptionError, match="choices"):
        caption_lines(transcript_of((0, 1, "a")), 0, 2, "comic-sans-rainbow")


# --- the table is the one list (drift) ---------------------------------------


def _choices(parser, option):
    for action in parser._actions:
        if option in action.option_strings:
            return list(action.choices)
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                found = _choices(sub, option)
                if found is not None:
                    return found
    return None


def test_both_cut_commands_offer_exactly_the_table():
    assert _choices(cut.build_parser(), "--caption-style") == style_names()
    assert _choices(run_clip.build_parser(), "--caption-style") == style_names()
    assert style_names() == list(caption_styles.STYLES)


# --- grouping and drawing per style -----------------------------------------


def test_words_per_line_follow_the_style():
    words = [(i * 0.3, i * 0.3 + 0.25, w) for i, w in enumerate("a b c d e f g h".split())]
    for name in style_names():
        lines = caption_lines(transcript_of(*words), 0, 5, name)
        assert max(len(line["words"]) for line in lines) == get_style(name).max_words_per_line


def test_caption_lines_default_to_the_default_style():
    transcript = transcript_of(*[(i * 0.3, i * 0.3 + 0.25, w) for i, w in enumerate("a b c d e".split())])
    assert caption_lines(transcript, 0, 5) == caption_lines(transcript, 0, 5, DEFAULT_STYLE)


@pytest.mark.parametrize("name", style_names())
def test_rendered_strips_are_the_style_height(tmp_path, name):
    lines = caption_lines(transcript_of((0.0, 0.3, "one"), (0.35, 0.6, "two")), 0, 2, name)
    timing = captions.render_captions(lines, tmp_path, 2.0, name)
    for png in timing.parent.glob("*.png"):
        with Image.open(png) as image:
            assert image.size == (1080, get_style(name).strip_height)


def test_the_box_style_draws_a_box_behind_the_lit_word_only(tmp_path):
    style = get_style(BOX_STYLE)
    lines = caption_lines(transcript_of((0.0, 0.3, "one"), (0.35, 0.6, "two")), 0, 2, BOX_STYLE)
    captions.render_captions(lines, tmp_path, 2.0, BOX_STYLE)

    def box_span(name):
        with Image.open(tmp_path / name) as image:
            rgba = image.convert("RGBA")
            row = [rgba.getpixel((x, style.strip_height // 2)) for x in range(rgba.size[0])]
            xs = [x for x, pixel in enumerate(row) if pixel == style.box_color]
            return min(xs), max(xs)

    first, second = box_span("line001-word1.png"), box_span("line001-word2.png")
    assert first[1] < second[0]
    assert first[1] - first[0] < 540 and second[1] - second[0] < 540
    with Image.open(tmp_path / "blank.png") as blank:
        assert blank.getextrema()[3] == (0, 0)


def test_composite_preview_draws_the_caption_over_the_frame(tmp_path):
    frame = solid_frame(tmp_path / "frame.png")
    style = get_style(DEFAULT_STYLE)

    out = composite_preview(frame, ["one", "two", "three"], 1, DEFAULT_STYLE, tmp_path / "out.png")

    with Image.open(out) as image:
        assert image.size == (1080, 1920)
        strip = image.crop((0, style.strip_top, 1080, style.strip_top + style.strip_height))
        colors = set(getattr(strip, "get_flattened_data", strip.getdata)())
        assert style.lit_text_color[:3] in colors
        assert image.getpixel((540, 100)) == (40, 90, 160)


def test_composite_preview_refuses_a_frame_of_the_wrong_size(tmp_path):
    frame = tmp_path / "frame.png"
    Image.new("RGB", (1920, 1080)).save(frame)
    with pytest.raises(CaptionError, match="1080x1920"):
        composite_preview(frame, ["a"], 0, DEFAULT_STYLE, tmp_path / "out.png")


# --- cutting in a style ------------------------------------------------------


@pytest.fixture
def project(dirs, tools):
    return Project(dirs, UNVERIFIED)


def test_a_box_style_cut_records_its_name_and_overlays_at_its_strip(project, tools):
    job_id = cut_highlights(SOURCE_ID, [1], captions=True, caption_style=BOX_STYLE)["staged"][0]["job_id"]

    manifest = project.manifest(job_id)
    assert manifest["captions"]["style"] == BOX_STYLE
    assert manifest["source"] == UNVERIFIED
    graph = tools.ffmpeg_calls[0][tools.ffmpeg_calls[0].index("-filter_complex") + 1]
    assert f"overlay=0:{get_style(BOX_STYLE).strip_top}:" in graph


def test_each_style_of_one_highlight_is_its_own_cut(project):
    jobs = [cut_highlights(SOURCE_ID, [1])["staged"][0]["job_id"]]
    for name in style_names():
        jobs.append(cut_highlights(SOURCE_ID, [1], captions=True, caption_style=name)["staged"][0]["job_id"])

    assert project.jobs() == sorted(jobs)
    assert "already cut" in refused(project, [1], captions=True, caption_style=BOX_STYLE)
    assert "already cut" in refused(project, [1], captions=True)
    assert "already cut" in refused(project, [1])


def test_a_captioned_manifest_with_an_unreadable_style_is_not_mistaken_for_plain(project):
    job_id = cut_highlights(SOURCE_ID, [1], captions=True)["staged"][0]["job_id"]
    path = project.staged / job_id / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["captions"] = "yes"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    plain = cut_highlights(SOURCE_ID, [1])["staged"][0]["job_id"]
    assert plain != job_id


def test_an_unknown_style_is_refused_before_anything_is_cut(project, tools):
    assert "unknown caption style" in refused(project, [1], captions=True, caption_style="neon")
    assert tools.ffmpeg_calls == []


def test_a_style_without_captions_is_refused_not_ignored(project, tools):
    assert "captions are off" in refused(project, [1], caption_style=BOX_STYLE)
    assert tools.ffmpeg_calls == []


def test_cut_commands_pass_the_style(project, monkeypatch, capsys):
    calls = []

    def fake_cut(source_id, only, profile, *, captions=False, caption_style=DEFAULT_STYLE):
        calls.append((captions, caption_style))
        return {"source_id": source_id, "profile": profile, "rights_basis": "own", "staged": [], "already_cut": []}

    monkeypatch.setattr(cut, "cut_highlights", fake_cut)
    monkeypatch.setattr(run_clip, "cut_highlights", fake_cut)

    cut.main([SOURCE_ID, "--captions", "--caption-style", BOX_STYLE])
    cut.main([SOURCE_ID, "--captions"])
    run_clip.main(["cut", SOURCE_ID, "--captions", "--caption-style", BOX_STYLE])
    run_clip.main(["cut", SOURCE_ID])

    assert calls == [
        (True, BOX_STYLE),
        (True, DEFAULT_STYLE),
        (True, BOX_STYLE),
        (False, DEFAULT_STYLE),
    ]


@pytest.mark.parametrize(
    "command",
    [
        lambda: cut.main([SOURCE_ID, "--caption-style", BOX_STYLE]),
        lambda: run_clip.main(["cut", SOURCE_ID, "--caption-style", BOX_STYLE]),
        lambda: cut.main([SOURCE_ID, "--captions", "--caption-style", "neon"]),
    ],
)
def test_a_style_without_captions_or_an_unknown_one_is_a_usage_error(project, tools, capsys, command):
    with pytest.raises(SystemExit) as stop:
        command()
    assert stop.value.code == 2
    assert tools.ffmpeg_calls == []


# --- preview -----------------------------------------------------------------


@pytest.fixture
def frame_grabs(monkeypatch, tools):
    """ffmpeg frame grabs answered with a real 1080x1920 PNG; other calls go to the stand-in."""
    grabs = []

    def run(cmd, **kwargs):
        if cmd[0] == "ffmpeg" and "-frames:v" in cmd:
            grabs.append(cmd)
            solid_frame(Path(cmd[-1]))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return tools(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return grabs


def test_preview_writes_one_still_per_style_in_the_work_folder(project, frame_grabs):
    paths = preview_styles(SOURCE_ID, 1)

    assert [path.name for path in paths] == [f"caption-preview-1-{name}.png" for name in style_names()]
    for path in paths:
        assert path.parent == project.work_dir
        with Image.open(path) as image:
            assert image.size == (1080, 1920)
    assert not (project.work_dir / "caption-preview-1-frame.png").exists()
    assert project.jobs() == []


def test_preview_grabs_a_scaled_frame_inside_the_highlight_while_a_word_is_spoken(project, frame_grabs):
    preview_styles(SOURCE_ID, 1)

    (cmd,) = frame_grabs
    assert cmd[cmd.index("-vf") + 1] == cut.SCALE_PAD_FILTER
    at = float(cmd[cmd.index("-ss") + 1])
    assert 0 <= at < 30
    assert str(project.source_dir / "source.mp4") in cmd


def test_preview_never_cuts_video(project, frame_grabs, tools):
    preview_styles(SOURCE_ID, 2)

    assert len(frame_grabs) == 1
    assert tools.ffmpeg_calls == []
    assert not project.staged.exists() or list(project.staged.iterdir()) == []


def test_preview_refuses_a_highlight_provenance_mismatch(project, frame_grabs):
    path = project.work_dir / "highlights.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["source"] = {**UNVERIFIED, "rights_basis": "own"}
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(CaptionError, match="provenance"):
        preview_styles(SOURCE_ID, 1)
    assert frame_grabs == []


def test_preview_refuses_a_transcript_provenance_mismatch(project, frame_grabs, monkeypatch):
    def stale_transcript(source_id):
        return project.work_dir, {"source": {**UNVERIFIED, "rights_basis": "own"}, "segments": []}

    monkeypatch.setattr(captions, "load_transcript", stale_transcript)

    with pytest.raises(CaptionError, match="provenance"):
        preview_styles(SOURCE_ID, 1)
    assert frame_grabs == []


def test_preview_refuses_an_unknown_highlight(project, frame_grabs):
    with pytest.raises(CaptionError, match="no highlight numbered 9"):
        preview_styles(SOURCE_ID, 9)


def test_preview_command_prints_the_paths(project, frame_grabs, capsys):
    assert captions.main(["preview", SOURCE_ID, "1"]) == 0
    out = capsys.readouterr().out
    for name in style_names():
        assert f"caption-preview-1-{name}.png" in out


def test_preview_command_reports_a_refusal(project, frame_grabs, capsys):
    assert captions.main(["preview", SOURCE_ID, "9"]) == 1
    assert "preview failed" in capsys.readouterr().err
