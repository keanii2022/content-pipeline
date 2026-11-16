"""The dashboard's Clip mode view and `run_clip status` read one description.

These tests cover that description (pipeline.clip.status) and the three
clip-mode actions the dashboard calls through pipeline.control.api. Tools
are stand-ins, as in test_clip_cut; nothing is decoded or published.
"""

import json
import re
from pathlib import Path

import pytest

from pipeline.clip import intake
from pipeline.clip.cut import cut_highlights
from pipeline.clip.highlights import record_highlights
from pipeline.clip.intake import IntakeError
from pipeline.clip.status import describe_source, list_sources
from pipeline.control import api
from pipeline.format.profiles import PROFILES
from pipeline.publish import youtube
from test_clip_cut import SOURCE_ID, UNVERIFIED, Project, dirs, project, tools  # noqa: F401

SERVER_JS = Path(__file__).resolve().parents[1] / "review-app" / "server.js"


def every_file(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def call_api(capsys, action, args):
    code = api.main([action, json.dumps(args)])
    return code, json.loads(capsys.readouterr().out)


# --- describing one source --------------------------------------------------


def test_a_source_that_is_only_taken_in_needs_transcribing(project):
    (project.work_dir / "highlights.json").unlink()
    (project.work_dir / "transcript.json").unlink()

    source = describe_source(SOURCE_ID)

    assert (source["transcribed"], source["highlights_recorded"]) == (False, False)
    assert source["highlights"] == []
    assert source["next"] == "transcribe"


def test_a_transcribed_source_needs_highlights(project):
    (project.work_dir / "highlights.json").unlink()

    source = describe_source(SOURCE_ID)

    assert source["transcribed"] is True
    assert (source["language"], source["segments"]) == ("en", 120)
    assert source["highlights_recorded"] is False
    assert source["next"] == "find-highlights"


def test_recorded_highlights_are_listed_in_order_and_need_cutting(project):
    source = describe_source(SOURCE_ID)

    assert [highlight["rank"] for highlight in source["highlights"]] == [1, 2, 3, 4, 5]
    assert source["highlights"][0] == {
        "rank": 1,
        "start": 0.0,
        "end": 30.0,
        "duration_seconds": 30.0,
        "hook_title": "Moment 0",
        "score": 90,
        "rationale": "Stands alone.",
        "job_id": None,
        "format_compliance": None,
        "cuts": [],
    }
    assert source["clips_cut"] == 0
    assert source["next"] == "cut"


def test_cut_highlights_point_at_their_staged_job_and_format_result(project):
    result = cut_highlights(SOURCE_ID, [2, 4])
    staged = {clip["rank"]: clip["job_id"] for clip in result["staged"]}

    source = describe_source(SOURCE_ID)

    assert {h["rank"]: h["job_id"] for h in source["highlights"]} == {
        1: None,
        2: staged[2],
        3: None,
        4: staged[4],
        5: None,
    }
    assert source["highlights"][1]["format_compliance"]["passed"] is True
    assert source["clips_cut"] == 2
    assert source["next"] == "cut"


def test_nothing_is_next_once_every_highlight_is_cut(project):
    cut_highlights(SOURCE_ID)

    source = describe_source(SOURCE_ID)

    assert source["clips_cut"] == 5
    assert source["next"] is None
    assert source["other_staged_clips"] == 0


def test_clips_from_replaced_picks_are_counted_apart(project):
    cut_highlights(SOURCE_ID, [1, 2])
    picks = [
        {"start": 40 + 100 * n, "end": 80 + 100 * n, "hook_title": f"New {n}", "score": 50, "rationale": "Ok."}
        for n in range(5)
    ]
    picks_path = project.root / "new-picks.json"
    picks_path.write_text(json.dumps(picks), encoding="utf-8")
    record_highlights(SOURCE_ID, picks_path)

    source = describe_source(SOURCE_ID)

    assert source["clips_cut"] == 0
    assert source["other_staged_clips"] == 2

    (project.work_dir / "highlights.json").unlink()
    assert describe_source(SOURCE_ID)["other_staged_clips"] == 2


def test_the_description_carries_the_source_provenance_unchanged(project):
    source = describe_source(SOURCE_ID)

    assert source["source"] == UNVERIFIED
    assert source["rights_basis"] == "unverified"
    assert source["origin"] == UNVERIFIED["source_url"]


def test_describing_a_source_changes_nothing_on_disk(project):
    cut_highlights(SOURCE_ID, [1])
    before = every_file(project.root)

    describe_source(SOURCE_ID)
    list_sources()

    assert every_file(project.root) == before


def test_a_source_claiming_rights_it_has_no_evidence_for_is_not_described(project):
    project.write_metadata({**UNVERIFIED, "rights_basis": "own"})

    with pytest.raises(IntakeError):
        describe_source(SOURCE_ID)


# --- listing every source ---------------------------------------------------


def test_sources_are_listed_newest_first_and_strays_are_ignored(project):
    raw = project.root / "raw" / "clip"
    newer = raw / "20261201T000000Z-ffffff"
    newer.mkdir()
    (newer / "metadata.json").write_text(
        (project.source_dir / "metadata.json")
        .read_text(encoding="utf-8")
        .replace(SOURCE_ID, newer.name),
        encoding="utf-8",
    )
    (newer / "source.mp4").write_bytes(b"video")
    (raw / "not-a-source").mkdir()
    (raw / ".DS_Store").write_bytes(b"")

    sources = list_sources()

    assert [source["source_id"] for source in sources] == [newer.name, SOURCE_ID]
    assert sources[0]["next"] == "transcribe"


def test_a_broken_source_is_listed_with_its_problem_not_hidden(project):
    project.write_metadata({**UNVERIFIED, "rights_basis": "own"})

    (listed,) = list_sources()

    assert listed["source_id"] == SOURCE_ID
    assert "own" in listed["error"]
    assert "rights_basis" not in listed  # nothing about it is vouched for


def test_one_unreadable_source_does_not_take_the_whole_list_down(project):
    other = project.root / "raw" / "clip" / "20261201T000000Z-ffffff"
    other.mkdir()
    (other / "metadata.json").write_bytes(b"\xff\xfe not text")

    sources = list_sources()

    assert [source["source_id"] for source in sources] == [other.name, SOURCE_ID]
    assert sources[0]["error"]
    assert "error" not in sources[1]


def test_a_staged_clip_with_mangled_times_is_ignored_not_fatal(project):
    result = cut_highlights(SOURCE_ID, [1])
    manifest_path = project.staged / result["staged"][0]["job_id"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["highlight"]["start"] = [0]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    source = describe_source(SOURCE_ID)

    assert source["clips_cut"] == 0
    assert len(cut_highlights(SOURCE_ID, [2])["staged"]) == 1


def test_no_sources_is_an_empty_list(dirs):
    assert list_sources() == []


# --- the actions the dashboard calls ----------------------------------------


def test_the_dashboard_can_list_sources(project, capsys):
    code, reply = call_api(capsys, "clip-sources", {})

    assert code == 0 and reply["ok"] is True
    assert [source["source_id"] for source in reply["result"]["sources"]] == [SOURCE_ID]


def test_the_dashboard_gets_one_source_and_the_profiles_it_can_check_against(project, capsys):
    code, reply = call_api(capsys, "clip-source", {"source_id": SOURCE_ID})

    assert code == 0
    assert reply["result"]["source"] == describe_source(SOURCE_ID)
    assert reply["result"]["profiles"] == sorted(PROFILES)
    assert reply["result"]["default_profile"] in PROFILES


@pytest.mark.parametrize("source_id", ["20261113T101500Z-000000", "../../etc"])
def test_asking_for_an_unknown_source_is_a_plain_error(project, capsys, source_id):
    code, reply = call_api(capsys, "clip-source", {"source_id": source_id})

    assert code == 1
    assert reply["ok"] is False and reply["error"]


def test_the_cut_button_cuts_the_chosen_highlights(project, capsys):
    code, reply = call_api(
        capsys, "clip-cut", {"source_id": SOURCE_ID, "indices": [3], "profile": "reels"}
    )

    assert code == 0
    (clip,) = reply["result"]["staged"]
    assert clip["rank"] == 3
    assert project.manifest(clip["job_id"])["format_compliance"]["profile"] == "reels"
    assert project.manifest(clip["job_id"])["source"] == UNVERIFIED


def test_the_cut_button_cuts_everything_left_by_default(project, capsys):
    cut_highlights(SOURCE_ID, [1])

    code, reply = call_api(capsys, "clip-cut", {"source_id": SOURCE_ID})

    assert code == 0
    assert [clip["rank"] for clip in reply["result"]["staged"]] == [2, 3, 4, 5]
    assert len(project.jobs()) == 5


def test_a_refused_cut_comes_back_as_an_error_and_stages_nothing(project, capsys):
    code, reply = call_api(capsys, "clip-cut", {"source_id": SOURCE_ID, "indices": [9]})

    assert code == 1
    assert reply["ok"] is False and "no highlight numbered" in reply["error"]
    assert project.jobs() == []


def test_a_cut_without_a_source_is_a_malformed_call(project, capsys):
    code, reply = call_api(capsys, "clip-cut", {"indices": [1]})

    assert code == 2
    assert "source_id" in reply["error"]


def test_the_caller_cannot_choose_which_programs_a_cut_runs(project, tools, capsys):
    code, _ = call_api(
        capsys,
        "clip-cut",
        {"source_id": SOURCE_ID, "indices": [1], "ffmpeg_path": "/tmp/evil", "ffprobe_path": "/tmp/evil"},
    )

    assert code == 0
    assert [cmd[0] for cmd in tools.ffmpeg_calls] == ["ffmpeg"]


def test_cutting_from_the_dashboard_does_not_publish(project, capsys, monkeypatch):
    def must_not_upload(*args, **kwargs):
        raise AssertionError("cutting must never reach the upload")

    monkeypatch.setattr(api, "upload_video", must_not_upload)
    monkeypatch.setattr(youtube, "upload_video", must_not_upload)

    code, _ = call_api(capsys, "clip-cut", {"source_id": SOURCE_ID})

    assert code == 0


# --- the server and the code it calls must agree ----------------------------


def test_every_action_the_server_offers_exists_in_the_control_api():
    server = SERVER_JS.read_text(encoding="utf-8")
    block = re.search(r"const CONTROL_ACTIONS = new Set\(\[(.*?)\]\);", server, re.DOTALL).group(1)
    offered = set(re.findall(r'^\s*"([a-z-]+)",', block, re.MULTILINE))
    called_directly = set(re.findall(r'respondWithControlAction\(res, "([a-z-]+)"', server))

    assert "clip-cut" in offered
    assert called_directly == {"clip-sources", "clip-source"}
    assert offered | called_directly <= set(api._ACTIONS)
    # Reading sources is not something a browser POST can trigger by name.
    assert not called_directly & offered


def test_the_server_accepts_exactly_the_source_ids_intake_makes():
    server = SERVER_JS.read_text(encoding="utf-8")
    pattern = re.search(r"const CLIP_SOURCE_ID = /(.+)/;", server).group(1)

    assert pattern == intake._SOURCE_ID.pattern
