"""Burned-in word-by-word captions for clip-mode cuts.

Caption timing is pure and tested directly. Cutting uses the stand-in
ffmpeg/ffprobe from test_clip_cut; the caption PNGs are drawn for real
with Pillow. Nothing is published.
"""

import builtins
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from pipeline.clip import captions, cut
from pipeline.clip.caption_styles import DEFAULT_STYLE, get_style
from pipeline.clip.captions import (
    BLANK_PNG,
    LINGER_SECONDS,
    TIMING_FILE,
    caption_lines,
    render_captions,
)
from pipeline.clip.cut import CutError, cut_highlights
from pipeline.orchestrate import run_clip
from pipeline.publish.youtube import YouTubePublishError, upload_video
from test_clip_cut import (  # noqa: F401
    SOURCE_ID,
    UNVERIFIED,
    Project,
    dirs,
    refused,
    tools,
)
from test_clip_publish import OWN, fake_youtube  # noqa: F401

DEFAULT = get_style(DEFAULT_STYLE)


def transcript_of(*words):
    """A transcript with one segment holding (start, end, text) words."""
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


def all_times(lines):
    for line in lines:
        yield line["start"]
        yield line["end"]
        for word in line["words"]:
            yield word["start"]
            yield word["end"]


def read_timing(path):
    """[(file, duration)] from an ffconcat file, with the repeated last file checked."""
    rows = path.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "ffconcat version 1.0"
    entries = []
    body = rows[1:]
    while len(body) >= 2:
        assert body[0].startswith("file ") and body[1].startswith("duration ")
        entries.append((body[0][5:], float(body[1][9:])))
        body = body[2:]
    assert body == [f"file {entries[-1][0]}"]
    return entries


# --- timing ------------------------------------------------------------------


def test_words_are_shifted_to_clip_time_and_grouped_in_threes():
    transcript = transcript_of(*[(100 + i * 0.4, 100.3 + i * 0.4, f"w{i}") for i in range(7)])

    lines = caption_lines(transcript, 100, 110)

    assert [[word["text"] for word in line["words"]] for line in lines] == [
        ["w0", "w1", "w2"],
        ["w3", "w4", "w5"],
        ["w6"],
    ]
    assert lines[0]["start"] == 0.0
    assert all(len(line["words"]) <= DEFAULT.max_words_per_line for line in lines)


def test_a_word_is_lit_until_the_next_word_and_a_line_clears_after_its_last():
    transcript = transcript_of((10.0, 10.2, "one"), (10.5, 10.7, "two"), (13.0, 13.4, "three."))

    lines = caption_lines(transcript, 10, 20)

    first, second = lines
    assert [(w["start"], w["end"]) for w in first["words"]] == [(0.0, 0.5), (0.5, first["end"])]
    assert first["end"] == pytest.approx(0.7 + LINGER_SECONDS)
    assert second["end"] == pytest.approx(3.4 + LINGER_SECONDS)


def test_a_line_clears_early_when_the_next_line_starts():
    transcript = transcript_of((0.0, 0.5, "end."), (0.6, 0.9, "next"))

    first, second = caption_lines(transcript, 0, 5)

    assert first["end"] == second["start"] == 0.6


def test_a_pause_starts_a_new_line():
    transcript = transcript_of((0.0, 0.2, "a"), (0.8, 1.0, "b"), (1.1, 1.3, "c"))

    lines = caption_lines(transcript, 0, 5)

    assert [[w["text"] for w in line["words"]] for line in lines] == [["a"], ["b", "c"]]


@pytest.mark.parametrize("ending", [".", "?", "!"])
def test_a_sentence_end_starts_a_new_line(ending):
    transcript = transcript_of((0.0, 0.2, f"done{ending}"), (0.25, 0.4, "then"))

    assert len(caption_lines(transcript, 0, 5)) == 2


def test_a_line_that_would_be_too_wide_breaks_early():
    transcript = transcript_of((0.0, 0.2, "extraordinarily"), (0.25, 0.4, "long"), (0.45, 0.6, "x"))

    lines = caption_lines(transcript, 0, 5)

    assert [[w["text"] for w in line["words"]] for line in lines] == [["extraordinarily"], ["long", "x"]]


def test_only_words_whose_midpoint_is_inside_the_highlight_are_kept():
    transcript = transcript_of(
        (8.0, 9.5, "before"),
        (9.4, 10.8, "straddles-in"),
        (19.8, 20.6, "straddles-out"),
        (20.5, 21.0, "after"),
    )

    lines = caption_lines(transcript, 10, 20)

    texts = [w["text"] for line in lines for w in line["words"]]
    assert texts == ["straddles-in"]
    assert lines[0]["words"][0]["start"] == 0.0  # clamped, not negative


def test_a_word_running_past_the_clip_end_is_clamped():
    transcript = transcript_of((19.5, 20.4, "last"))

    lines = caption_lines(transcript, 10, 20)

    assert max(all_times(lines)) <= 10.0
    assert lines[-1]["end"] == 10.0


def test_out_of_order_and_overlapping_words_never_go_backwards():
    transcript = transcript_of(
        (3.0, 3.5, "c"), (1.0, 2.5, "a"), (1.5, 2.0, "b"), (1.5, 1.6, "b2"), (3.2, 3.1, "bad")
    )

    lines = caption_lines(transcript, 0, 4)

    times = [t for line in lines for w in line["words"] for t in (w["start"], w["end"])]
    assert times == sorted(times)
    assert all(0.0 <= t <= 4.0 for t in all_times(lines))
    for line in lines:
        assert line["start"] <= line["end"]
    for earlier, later in zip(lines, lines[1:]):
        assert earlier["end"] <= later["start"]


def test_words_with_no_usable_time_or_text_are_skipped():
    transcript = {
        "segments": [
            {"words": [{"start": "1", "end": 2, "word": "x"}, {"start": 1, "end": 2, "word": "  "}]},
            {"words": [{"start": True, "end": 2, "word": "y"}, {"start": 1.0, "end": 1.2, "word": "ok"}]},
            "not a segment",
        ]
    }

    lines = caption_lines(transcript, 0, 5)

    assert [w["text"] for line in lines for w in line["words"]] == ["ok"]


def test_no_words_in_the_range_means_no_lines():
    assert caption_lines(transcript_of((50.0, 50.5, "far")), 0, 10) == []


# --- rendering ---------------------------------------------------------------


def test_one_png_per_lit_word_plus_a_blank_all_the_same_size(tmp_path):
    transcript = transcript_of(*[(1 + i * 0.4, 1.3 + i * 0.4, f"word{i}") for i in range(5)])
    lines = caption_lines(transcript, 0, 6)

    timing = render_captions(lines, tmp_path / "captions", 6.0)

    pngs = sorted(timing.parent.glob("*.png"))
    assert len(pngs) == sum(len(line["words"]) for line in lines) + 1
    assert (timing.parent / BLANK_PNG) in pngs
    for png in pngs:
        with Image.open(png) as image:
            assert image.size == (1080, DEFAULT.strip_height)
            assert image.mode == "RGBA"


def test_timing_durations_add_up_to_the_clip_length(tmp_path):
    transcript = transcript_of(
        (10.05, 10.333, "a"), (10.4, 10.9, "b."), (12.0, 12.25, "c"), (19.9, 20.4, "d")
    )
    lines = caption_lines(transcript, 10, 20)

    entries = read_timing(render_captions(lines, tmp_path, 10.0))

    assert round(sum(duration for _, duration in entries), 3) == 10.0
    assert all(duration > 0 for _, duration in entries)
    assert entries[0][0] == BLANK_PNG
    for name, _ in entries:
        assert (tmp_path / name).is_file()


def test_the_lit_word_is_yellow_and_the_rest_white(tmp_path):
    lines = caption_lines(transcript_of((0.0, 0.3, "one"), (0.35, 0.6, "two")), 0, 2)
    render_captions(lines, tmp_path, 2.0)

    def colors(name):
        with Image.open(tmp_path / name) as image:
            pixels = getattr(image, "get_flattened_data", image.getdata)()
            return {pixel[:3] for pixel in pixels if pixel[3] == 255}

    first = colors("line001-word1.png")
    assert DEFAULT.lit_text_color[:3] in first
    assert DEFAULT.text_color[:3] in first
    with Image.open(tmp_path / BLANK_PNG) as blank:
        assert blank.getextrema()[3] == (0, 0)


def test_the_bundled_font_and_its_license_are_present():
    assert captions.FONT_PATH.is_file()
    assert (captions.FONT_PATH.parent / "OFL.txt").is_file()


# --- cutting with captions ---------------------------------------------------


@pytest.fixture
def project(dirs, tools):
    return Project(dirs, UNVERIFIED)


def test_captions_are_off_by_default_and_the_command_is_unchanged(project, tools):
    cut_highlights(SOURCE_ID, [1])

    source = project.source_dir / "source.mp4"
    (cmd,) = tools.ffmpeg_calls
    assert cmd[:-1] == [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", "0.000",
        "-t", "30.000",
        "-i", str(source),
        "-map", "0:V:0",
        "-map", "0:a:0",
        "-map_chapters", "-1",
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-f", "mp4",
    ]


def test_a_captioned_cut_overlays_the_caption_timeline(project, tools, monkeypatch):
    seen = {}
    real_run = subprocess.run

    def check_build_dir(cmd, **kwargs):
        if cmd[0] == "ffmpeg":
            timing = Path(cmd[cmd.index("concat") + 4])
            seen["entries"] = read_timing(timing)
            seen["pngs"] = len(list(timing.parent.glob("*.png")))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", check_build_dir)

    job_id = cut_highlights(SOURCE_ID, [1], captions=True)["staged"][0]["job_id"]

    (cmd,) = tools.ffmpeg_calls
    assert cmd[cmd.index("-i") + 2:cmd.index("-i") + 6] == ["-f", "concat", "-safe", "0"]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.startswith("[0:V:0]scale=1080:1920:force_original_aspect_ratio=decrease,")
    assert f"overlay=0:{DEFAULT.strip_top}:eof_action=pass[v]" in graph
    assert cmd[cmd.index("-map") + 1] == "[v]"
    assert "0:a:0" in cmd
    assert "-vf" not in cmd
    assert round(sum(d for _, d in seen["entries"]), 3) == 30.0
    assert seen["pngs"] > 1
    assert sorted(p.name for p in (project.staged / job_id).iterdir()) == ["manifest.json", "output.mp4"]
    assert project.leftovers() == []


def test_a_captioned_manifest_says_so_and_keeps_provenance_unchanged(project):
    job_id = cut_highlights(SOURCE_ID, [1], captions=True)["staged"][0]["job_id"]

    manifest = project.manifest(job_id)
    assert manifest["source"] == UNVERIFIED
    # The project's transcript has back-to-back 2 s words: 15 words, 3 per line.
    assert manifest["captions"] == {"style": DEFAULT_STYLE, "lines": 5}


def test_a_plain_manifest_has_no_captions_key(project):
    job_id = cut_highlights(SOURCE_ID, [1])["staged"][0]["job_id"]

    assert "captions" not in project.manifest(job_id)


def test_a_captioned_and_a_plain_cut_of_one_highlight_can_both_exist(project):
    plain = cut_highlights(SOURCE_ID, [1])["staged"][0]["job_id"]
    captioned = cut_highlights(SOURCE_ID, [1], captions=True)["staged"][0]["job_id"]

    assert plain != captioned
    assert project.jobs() == sorted([plain, captioned])
    assert "already cut" in refused(project, [1], captions=True)
    assert "already cut" in refused(project, [1])


def test_cutting_everything_with_captions_skips_only_captioned_ones(project):
    cut_highlights(SOURCE_ID, [2], captions=True)

    result = cut_highlights(SOURCE_ID, captions=True)

    assert [clip["rank"] for clip in result["staged"]] == [1, 3, 4, 5]
    assert [item["rank"] for item in result["already_cut"]] == [2]


def test_captions_without_a_transcript_are_refused(project, tools):
    (project.work_dir / "transcript.json").unlink()

    assert "transcript" in refused(project, [1], captions=True)
    assert tools.ffmpeg_calls == []


def test_captions_with_no_words_in_the_highlight_are_refused(project, tools):
    path = project.work_dir / "transcript.json"
    transcript = json.loads(path.read_text(encoding="utf-8"))
    for segment in transcript["segments"]:
        if 100 <= segment["start"] < 130:
            segment["words"] = []
    path.write_text(json.dumps(transcript), encoding="utf-8")

    message = refused(project, [1, 2], captions=True)

    assert "#2" in message and "no transcribed words" in message
    assert tools.ffmpeg_calls == []


def test_a_transcript_with_different_provenance_is_refused(project, tools):
    path = project.work_dir / "transcript.json"
    transcript = json.loads(path.read_text(encoding="utf-8"))
    transcript["source"] = {**UNVERIFIED, "rights_basis": "own"}
    path.write_text(json.dumps(transcript), encoding="utf-8")

    assert "provenance" in refused(project, [1], captions=True)
    assert tools.ffmpeg_calls == []


def test_a_transcript_provenance_mismatch_is_caught_by_cut_itself(project, tools, monkeypatch):
    def stale_transcript(source_id):
        return project.work_dir, {"source": {**UNVERIFIED, "rights_basis": "own"}, "segments": []}

    monkeypatch.setattr(cut, "load_transcript", stale_transcript)

    assert "provenance" in refused(project, [1], captions=True)


def test_missing_pillow_is_a_clear_error_before_anything_is_cut(project, tools, monkeypatch):
    real_import = builtins.__import__

    def no_pillow(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("No module named 'PIL'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pillow)

    message = refused(project, [1], captions=True)

    assert "Pillow" in message and ".[captions]" in message
    assert tools.ffmpeg_calls == []


@pytest.mark.parametrize("not_a_bool", ["yes", 1, None])
def test_captions_must_be_a_real_bool(project, tools, not_a_bool):
    refused(project, [1], captions=not_a_bool)


def test_a_failed_captioned_cut_leaves_nothing_behind(project, tools):
    tools.fail_ffmpeg_on_call = 1

    refused(project, [1], captions=True)


# --- command line ------------------------------------------------------------


def test_cut_command_passes_captions(project, monkeypatch, capsys):
    calls = []

    def fake_cut(source_id, only, profile, *, captions=False, caption_style=DEFAULT_STYLE):
        calls.append(captions)
        return {"source_id": source_id, "profile": profile, "rights_basis": "own", "staged": [], "already_cut": []}

    monkeypatch.setattr(cut, "cut_highlights", fake_cut)
    monkeypatch.setattr(run_clip, "cut_highlights", fake_cut)

    cut.main([SOURCE_ID, "--captions"])
    cut.main([SOURCE_ID])
    run_clip.main(["cut", SOURCE_ID, "--only", "1", "--captions"])
    run_clip.main(["cut", SOURCE_ID])

    assert calls == [True, False, True, False]


# --- publishing --------------------------------------------------------------


def test_a_captioned_clip_publishes_under_the_same_rules(dirs, tools, fake_youtube):
    project = Project(dirs, UNVERIFIED)
    job_id = cut_highlights(SOURCE_ID, [1], captions=True)["staged"][0]["job_id"]

    with pytest.raises(YouTubePublishError, match="unverified"):
        upload_video(job_id, title="A title I typed")
    assert fake_youtube.uploads == []

    upload_video(job_id, title="A title I typed", acknowledge_unverified_rights=True)
    assert len(fake_youtube.uploads) == 1
    assert project.manifest(job_id)["captions"]["style"] == DEFAULT_STYLE


def test_a_captioned_clip_with_tampered_provenance_is_still_refused(dirs, tools, fake_youtube):
    project = Project(dirs, OWN)
    job_id = cut_highlights(SOURCE_ID, [1], captions=True)["staged"][0]["job_id"]
    path = project.staged / job_id / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    del manifest["source"]
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(YouTubePublishError, match="provenance"):
        upload_video(job_id, title="A title I typed")
    assert fake_youtube.uploads == []
