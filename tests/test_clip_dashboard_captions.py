"""Captions on the dashboard's Cut button (Step 25).

The control API turns captions on only for a literal JSON true, the
dashboard reads the caption looks from the API instead of keeping its own
list, and a highlight's plain and captioned cuts are all described.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest

from pipeline.clip import caption_styles, captions, cut, status
from pipeline.clip.caption_styles import DEFAULT_STYLE, style_names
from pipeline.clip.cut import cut_highlights
from pipeline.clip.status import describe_source
from pipeline.control import api
from pipeline.publish import youtube
from test_clip_cut import SOURCE_ID, UNVERIFIED, Project, dirs, tools  # noqa: F401

APP_JS = Path(__file__).resolve().parents[1] / "review-app" / "public" / "app.js"
OTHER_STYLE = next(name for name in style_names() if name != DEFAULT_STYLE)


@pytest.fixture
def project(dirs, tools):
    return Project(dirs, UNVERIFIED)


def call_api(capsys, action, args):
    code = api.main([action, json.dumps(args)])
    return code, json.loads(capsys.readouterr().out)


# --- the control API --------------------------------------------------------


def test_clip_source_offers_the_caption_styles_and_the_default(project, capsys):
    code, reply = call_api(capsys, "clip-source", {"source_id": SOURCE_ID})

    assert code == 0
    assert reply["result"]["caption_styles"] == style_names()
    assert reply["result"]["default_caption_style"] == DEFAULT_STYLE


def test_a_literal_true_cuts_with_captions_in_the_chosen_style(project, capsys):
    code, reply = call_api(
        capsys,
        "clip-cut",
        {"source_id": SOURCE_ID, "indices": [1], "captions": True, "caption_style": OTHER_STYLE},
    )

    assert code == 0
    (clip,) = reply["result"]["staged"]
    assert project.manifest(clip["job_id"])["captions"]["style"] == OTHER_STYLE
    assert project.manifest(clip["job_id"])["source"] == UNVERIFIED


def test_captions_true_without_a_style_uses_the_default(project, capsys):
    code, reply = call_api(capsys, "clip-cut", {"source_id": SOURCE_ID, "indices": [1], "captions": True})

    assert code == 0
    (clip,) = reply["result"]["staged"]
    assert project.manifest(clip["job_id"])["captions"]["style"] == DEFAULT_STYLE


@pytest.mark.parametrize("not_true", ["true", 1, "yes", None, False])
def test_anything_but_a_literal_true_cuts_a_plain_clip(project, capsys, not_true):
    code, reply = call_api(capsys, "clip-cut", {"source_id": SOURCE_ID, "indices": [1], "captions": not_true})

    assert code == 0
    (clip,) = reply["result"]["staged"]
    assert "captions" not in project.manifest(clip["job_id"])


@pytest.mark.parametrize("not_true", ["true", 1])
def test_a_style_with_a_not_quite_true_captions_is_refused(project, capsys, not_true):
    code, reply = call_api(
        capsys,
        "clip-cut",
        {"source_id": SOURCE_ID, "indices": [1], "captions": not_true, "caption_style": OTHER_STYLE},
    )

    assert code == 1
    assert "captions are off" in reply["error"]
    assert project.jobs() == []


@pytest.mark.parametrize("bad_style", ["comic-sans", 3, ["bold-white-yellow-word"], None])
def test_an_unusable_caption_style_is_refused(project, capsys, bad_style):
    code, reply = call_api(
        capsys,
        "clip-cut",
        {"source_id": SOURCE_ID, "indices": [1], "captions": True, "caption_style": bad_style},
    )

    assert code == 1
    assert reply["ok"] is False and "caption" in reply["error"]
    assert project.jobs() == []


def test_a_captioned_cut_from_the_dashboard_does_not_publish(project, capsys, monkeypatch):
    def must_not_upload(*args, **kwargs):
        raise AssertionError("cutting must never reach the upload")

    monkeypatch.setattr(api, "upload_video", must_not_upload)
    monkeypatch.setattr(youtube, "upload_video", must_not_upload)

    code, _ = call_api(capsys, "clip-cut", {"source_id": SOURCE_ID, "captions": True})

    assert code == 0


@pytest.mark.parametrize("module", [cut, captions, caption_styles, status])
def test_nothing_in_the_cut_path_imports_publishing(module):
    tree = ast.parse(inspect.getsource(module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not any(name.startswith("pipeline.publish") for name in imported)


def test_the_cut_action_never_mentions_uploading():
    source = inspect.getsource(api._clip_cut)

    assert "upload" not in source and "youtube" not in source


# --- describing several cuts of one highlight -------------------------------


def test_a_plain_and_captioned_cuts_of_one_highlight_are_all_listed(project):
    plain = cut_highlights(SOURCE_ID, [1])["staged"][0]["job_id"]
    default = cut_highlights(SOURCE_ID, [1], captions=True)["staged"][0]["job_id"]
    other = cut_highlights(SOURCE_ID, [1], captions=True, caption_style=OTHER_STYLE)["staged"][0]["job_id"]
    second = cut_highlights(SOURCE_ID, [2], captions=True)["staged"][0]["job_id"]

    source = describe_source(SOURCE_ID)
    first, two = source["highlights"][0], source["highlights"][1]

    assert {(c["job_id"], c["captions"]) for c in first["cuts"]} == {
        (plain, None),
        (default, DEFAULT_STYLE),
        (other, OTHER_STYLE),
    }
    assert first["cuts"][0]["captions"] is None
    assert first["job_id"] == plain
    assert all(c["format_compliance"]["passed"] is True for c in first["cuts"])
    assert [(c["job_id"], c["captions"]) for c in two["cuts"]] == [(second, DEFAULT_STYLE)]
    assert two["job_id"] == second
    assert source["clips_cut"] == 2
    assert source["cuts_staged"] == 4
    assert source["other_staged_clips"] == 0
    assert source["next"] == "cut"


def test_extra_looks_do_not_count_as_clips_from_replaced_picks(project):
    cut_highlights(SOURCE_ID)
    cut_highlights(SOURCE_ID, captions=True)

    source = describe_source(SOURCE_ID)

    assert source["clips_cut"] == 5
    assert source["cuts_staged"] == 10
    assert source["other_staged_clips"] == 0
    assert source["next"] is None


# --- the dashboard and the API must agree -----------------------------------


def test_the_dashboard_reads_the_caption_styles_from_the_api():
    app = APP_JS.read_text(encoding="utf-8")

    assert [name for name in style_names() if name in app] == []
    assert "data.caption_styles" in app
    assert "data.default_caption_style" in app


def test_the_captions_box_starts_unticked():
    app = APP_JS.read_text(encoding="utf-8")

    assert re.search(r"^const CAPTIONS_ON_BY_DEFAULT = false;$", app, re.MULTILINE)
    assert re.search(r"let captionChoice = \{ on: CAPTIONS_ON_BY_DEFAULT,", app)


def test_the_dashboard_sends_captions_as_a_real_boolean():
    app = APP_JS.read_text(encoding="utf-8")

    assert "args.captions = true;" in app
