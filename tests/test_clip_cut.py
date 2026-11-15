"""Cutting recorded highlights into staged clips.

ffmpeg and ffprobe are stand-ins: the fake ffmpeg writes a few bytes where
the clip would go, the fake ffprobe describes a compliant 9:16 file. No
video is decoded and nothing is published.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from pipeline.clip import cut, intake, transcribe as transcribe_module
from pipeline.clip.cut import CutError, cut_highlights
from pipeline.clip.highlights import record_highlights
from pipeline.format import validate as format_validate
from pipeline.publish.youtube import YouTubePublishError, _check_eligibility

SOURCE_ID = "20261113T101500Z-a1b2c3"
JOB_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")

UNVERIFIED = {
    "source_kind": "url",
    "source_url": "https://www.youtube.com/watch?v=abc",
    "original_filename": None,
    "title": "A long podcast",
    "uploader": "Some Podcast",
    "uploader_url": "https://www.youtube.com/@somepodcast",
    "channel_id": "UCxxxx",
    "channel_url": "https://www.youtube.com/channel/UCxxxx",
    "license": None,
    "rights_basis": "unverified",
    "permission_ledger_reference": None,
}
ALLOWLISTED = {
    **UNVERIFIED,
    "rights_basis": "allowlist",
    "permission_ledger_reference": {
        "creator_id": "somepodcast",
        "source_url": "https://www.youtube.com/@somepodcast",
        "permission_granted_date": "2026-09-13",
    },
}

GOOD_PROBE = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
    "format": {"duration": "30.0"},
}


class FakeTools:
    """Stands in for subprocess.run: answers ffmpeg and ffprobe, keeps the calls."""

    def __init__(self):
        self.ffmpeg_calls = []
        self.probe = GOOD_PROBE
        self.fail_ffmpeg_on_call = None
        self.interrupt_ffmpeg = False

    def __call__(self, cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(self.probe), stderr="")
        assert cmd[0] == "ffmpeg", f"unexpected tool: {cmd[0]}"
        self.ffmpeg_calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"half a clip")
        if self.interrupt_ffmpeg:
            raise KeyboardInterrupt
        if self.fail_ffmpeg_on_call == len(self.ffmpeg_calls):
            raise subprocess.CalledProcessError(1, cmd, stderr="Conversion failed!")
        Path(cmd[-1]).write_bytes(b"a finished clip")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def make_transcript(provenance, duration=1200):
    segments = []
    for start in range(0, duration, 10):
        words = [
            {"start": float(t), "end": float(t + 2), "word": f"w{t}"}
            for t in range(start, start + 10, 2)
        ]
        segments.append({"start": float(start), "end": float(start + 10), "text": "words", "words": words})
    return {
        "source_id": SOURCE_ID,
        "language": "en",
        "duration_seconds": float(duration),
        "whisper_model": "small",
        "source": provenance,
        "segments": segments,
    }


class Project:
    """A source that has been taken in, transcribed and had five highlights recorded."""

    def __init__(self, root, provenance):
        self.root = root
        self.source_dir = root / "raw" / "clip" / SOURCE_ID
        self.work_dir = root / "work" / f"clip-{SOURCE_ID}"
        self.staged = root / "staged"
        self.source_dir.mkdir(parents=True)
        self.work_dir.mkdir(parents=True)
        (self.source_dir / "source.mp4").write_bytes(b"video")
        self.write_metadata(provenance)
        (self.work_dir / "transcript.json").write_text(
            json.dumps(make_transcript(provenance)), encoding="utf-8"
        )
        (self.work_dir / "transcript.txt").write_text("[00:00] words\n", encoding="utf-8")
        picks = [
            {
                "start": 100 * n,
                "end": 100 * n + 30,
                "hook_title": f"Moment {n}",
                "score": 90 - n,
                "rationale": "Stands alone.",
            }
            for n in range(5)
        ]
        picks_path = root / "agent-output.json"
        picks_path.write_text(json.dumps(picks), encoding="utf-8")
        self.highlights_path = record_highlights(SOURCE_ID, picks_path)

    def write_metadata(self, provenance):
        metadata = {
            **provenance,
            "source_id": SOURCE_ID,
            "duration_seconds": 1200.0,
            "source_file": "source.mp4",
            "fetched_at": "2026-11-13T10:15:00+00:00",
        }
        (self.source_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    def edit_highlights(self, change):
        record = json.loads(self.highlights_path.read_text(encoding="utf-8"))
        change(record)
        self.highlights_path.write_text(json.dumps(record), encoding="utf-8")

    def jobs(self):
        if not self.staged.is_dir():
            return []
        return sorted(path.name for path in self.staged.iterdir())

    def manifest(self, job_id):
        return json.loads((self.staged / job_id / "manifest.json").read_text(encoding="utf-8"))

    def leftovers(self):
        return [path.name for path in self.work_dir.iterdir() if path.name.startswith("cut-")]


@pytest.fixture
def tools(monkeypatch):
    fake = FakeTools()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "RAW_CLIP_DIR", tmp_path / "raw" / "clip")
    monkeypatch.setattr(transcribe_module, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(format_validate, "STAGED_DIR", tmp_path / "staged")
    return tmp_path


@pytest.fixture
def project(dirs, tools):
    return Project(dirs, UNVERIFIED)


@pytest.fixture
def allowlisted(dirs, tools, monkeypatch):
    monkeypatch.setattr(cut, "is_permitted", lambda creator_id, source_url: True)
    return Project(dirs, ALLOWLISTED)


def refused(project, *args, **kwargs):
    """The cut is refused and nothing new reaches staged/ or is left half-built."""
    before = project.jobs()
    with pytest.raises(CutError) as refusal:
        cut_highlights(SOURCE_ID, *args, **kwargs)
    assert project.jobs() == before
    assert project.leftovers() == []
    return str(refusal.value)


# --- what a cut produces ----------------------------------------------------


def test_every_highlight_becomes_its_own_staged_job(project):
    result = cut_highlights(SOURCE_ID)

    assert [clip["rank"] for clip in result["staged"]] == [1, 2, 3, 4, 5]
    assert project.jobs() == sorted(clip["job_id"] for clip in result["staged"])
    assert len(set(project.jobs())) == 5
    for clip in result["staged"]:
        assert JOB_ID.match(clip["job_id"])
        job_dir = project.staged / clip["job_id"]
        assert (job_dir / "output.mp4").read_bytes() == b"a finished clip"
        manifest = project.manifest(clip["job_id"])
        assert manifest["job_id"] == clip["job_id"]
        assert manifest["mode"] == "clip"
        assert manifest["output_file"] == "output.mp4"
        assert manifest["source_id"] == SOURCE_ID
        assert manifest["assembled_at"]
    assert project.leftovers() == []


def test_manifest_records_the_highlight_it_was_cut_from(project):
    result = cut_highlights(SOURCE_ID, [2])

    manifest = project.manifest(result["staged"][0]["job_id"])
    assert manifest["highlight"] == {
        "rank": 2,
        "start": 100.0,
        "end": 130.0,
        "duration_seconds": 30.0,
        "hook_title": "Moment 1",
        "score": 89,
        "rationale": "Stands alone.",
    }


def test_format_check_result_is_recorded_in_each_manifest(project):
    result = cut_highlights(SOURCE_ID, [1], "reels")

    compliance = project.manifest(result["staged"][0]["job_id"])["format_compliance"]
    assert compliance["profile"] == "reels"
    assert compliance["passed"] is True
    assert result["staged"][0]["format_compliance"] == compliance


def test_a_clip_that_fails_the_format_check_is_staged_with_the_failure(project, tools):
    tools.probe = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "30.0"},
    }

    result = cut_highlights(SOURCE_ID, [1])

    compliance = project.manifest(result["staged"][0]["job_id"])["format_compliance"]
    assert compliance["passed"] is False
    assert any("aspect ratio" in reason for reason in compliance["reasons"])


# --- how ffmpeg is asked to cut ---------------------------------------------


def test_ffmpeg_cuts_the_highlight_and_keeps_the_source_audio(project, tools):
    cut_highlights(SOURCE_ID, [2])

    (cmd,) = tools.ffmpeg_calls
    assert cmd[cmd.index("-ss") + 1] == "100.000"
    assert cmd[cmd.index("-t") + 1] == "30.000"
    # One input only: the source. Its own audio track is the clip's audio.
    assert cmd.count("-i") == 1
    assert cmd[cmd.index("-i") + 1] == str(project.source_dir / "source.mp4")
    assert cmd.index("-ss") < cmd.index("-i")
    maps = [cmd[i + 1] for i, part in enumerate(cmd) if part == "-map"]
    assert maps == ["0:V:0", "0:a:0"]
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-c:a") + 1] == "aac"


def test_ffmpeg_fits_the_picture_into_1080x1920_without_cropping(project, tools):
    cut_highlights(SOURCE_ID, [1])

    video_filter = tools.ffmpeg_calls[0][tools.ffmpeg_calls[0].index("-vf") + 1]
    assert "scale=1080:1920:force_original_aspect_ratio=decrease" in video_filter
    assert "pad=1080:1920:(ow-iw)/2:(oh-ih)/2" in video_filter
    assert "crop" not in video_filter


# --- choosing which highlights to cut ---------------------------------------


def test_indices_pick_highlights_by_their_number(project):
    result = cut_highlights(SOURCE_ID, [4, 2])

    assert [clip["rank"] for clip in result["staged"]] == [2, 4]
    assert len(project.jobs()) == 2


@pytest.mark.parametrize(
    "indices", [[], [0], [6], [1, 1], [True], ["1"], [1.0], "12", 3]
)
def test_a_bad_selection_is_refused_before_anything_is_cut(project, tools, indices):
    refused(project, indices)
    assert tools.ffmpeg_calls == []


def test_an_unknown_profile_is_refused_before_anything_is_cut(project, tools):
    assert "unknown format profile" in refused(project, None, "vine")
    assert tools.ffmpeg_calls == []


def test_cutting_needs_recorded_highlights(project, tools):
    project.highlights_path.unlink()

    assert "no highlights recorded" in refused(project)
    assert tools.ffmpeg_calls == []


# --- never cut the same highlight twice, never overwrite --------------------


def test_cutting_again_only_cuts_what_is_not_staged_yet(project):
    first = cut_highlights(SOURCE_ID, [1, 3])
    second = cut_highlights(SOURCE_ID)

    assert [clip["rank"] for clip in second["staged"]] == [2, 4, 5]
    assert second["already_cut"] == [
        {"rank": 1, "job_id": first["staged"][0]["job_id"]},
        {"rank": 3, "job_id": first["staged"][1]["job_id"]},
    ]
    assert len(project.jobs()) == 5


def test_naming_a_highlight_that_is_already_staged_is_refused(project):
    first = cut_highlights(SOURCE_ID, [1])

    message = refused(project, [1, 2])
    assert first["staged"][0]["job_id"] in message


def test_cutting_everything_twice_is_refused(project):
    cut_highlights(SOURCE_ID)

    assert "already cut" in refused(project)


def test_an_existing_staged_job_is_never_overwritten(project, monkeypatch):
    taken = project.staged / "20261113T120000Z-abcdef"
    taken.mkdir(parents=True)
    (taken / "output.mp4").write_bytes(b"someone else's finished video")
    monkeypatch.setattr(cut, "_new_job_id", lambda: taken.name)

    assert "already exists" in refused(project, [1])
    assert (taken / "output.mp4").read_bytes() == b"someone else's finished video"
    assert not (taken / "manifest.json").exists()


def test_a_broken_manifest_in_another_job_does_not_block_cutting(project):
    other = project.staged / "20261113T120000Z-abcdef"
    other.mkdir(parents=True)
    (other / "manifest.json").write_text("{not json", encoding="utf-8")

    assert len(cut_highlights(SOURCE_ID, [1])["staged"]) == 1


# --- failures leave nothing half-staged -------------------------------------


def test_a_failed_cut_stages_nothing_and_keeps_earlier_clips(project, tools):
    tools.fail_ffmpeg_on_call = 2

    with pytest.raises(CutError) as failure:
        cut_highlights(SOURCE_ID)

    (kept,) = project.jobs()
    assert project.manifest(kept)["highlight"]["rank"] == 1
    assert "highlight #2" in str(failure.value)
    assert kept in str(failure.value)
    assert project.leftovers() == []


def test_stopping_mid_cut_leaves_nothing_behind(project, tools):
    tools.interrupt_ffmpeg = True

    with pytest.raises(KeyboardInterrupt):
        cut_highlights(SOURCE_ID, [1])

    assert project.jobs() == []
    assert project.leftovers() == []


def test_missing_ffmpeg_is_reported_plainly(project, monkeypatch):
    def no_such_tool(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", no_such_tool)

    assert "is ffmpeg installed" in refused(project, [1])


# --- provenance and rights_basis travel with every clip ---------------------


def test_provenance_is_copied_unchanged_into_the_manifest(project):
    result = cut_highlights(SOURCE_ID, [1])

    manifest = project.manifest(result["staged"][0]["job_id"])
    assert manifest["source"] == UNVERIFIED
    assert manifest["source"]["rights_basis"] == "unverified"
    assert result["rights_basis"] == "unverified"


def test_an_allowlisted_source_keeps_its_ledger_reference(allowlisted):
    result = cut_highlights(SOURCE_ID, [1])

    manifest = allowlisted.manifest(result["staged"][0]["job_id"])
    assert manifest["source"] == ALLOWLISTED


def test_allowlist_is_checked_again_against_the_ledger_before_cutting(
    allowlisted, tools, monkeypatch
):
    asked = []

    def lapsed(creator_id, source_url):
        asked.append((creator_id, source_url))
        return False

    monkeypatch.setattr(cut, "is_permitted", lapsed)

    assert "no longer permits" in refused(allowlisted)
    assert asked == [("somepodcast", "https://www.youtube.com/@somepodcast")]
    assert tools.ffmpeg_calls == []


def test_an_unreadable_ledger_stops_an_allowlisted_cut(allowlisted, tools, monkeypatch):
    def broken(creator_id, source_url):
        raise cut.LedgerError("ledger is malformed")

    monkeypatch.setattr(cut, "is_permitted", broken)

    assert "ledger" in refused(allowlisted)
    assert tools.ffmpeg_calls == []


def test_an_unverified_source_never_touches_the_ledger(project, monkeypatch):
    def must_not_be_called(*args, **kwargs):
        raise AssertionError("the ledger is not a gate for unverified sources")

    monkeypatch.setattr(cut, "is_permitted", must_not_be_called)

    assert len(cut_highlights(SOURCE_ID, [1])["staged"]) == 1


def test_highlights_claiming_better_rights_than_the_source_are_refused(project, tools):
    project.edit_highlights(lambda record: record["source"].update(rights_basis="own"))

    assert "provenance" in refused(project)
    assert tools.ffmpeg_calls == []


def test_metadata_edited_to_claim_better_rights_is_refused(project, tools):
    project.write_metadata({**UNVERIFIED, "rights_basis": "own"})

    refused(project)
    assert tools.ffmpeg_calls == []


def test_a_ledger_reference_for_someone_elses_channel_is_refused(allowlisted, tools):
    """An edited metadata.json can't borrow a real ledger entry: the entry
    has to be for the channel this video actually came from."""
    borrowed = {
        **ALLOWLISTED,
        "uploader_url": "https://www.youtube.com/@someoneelse",
        "channel_url": "https://www.youtube.com/channel/UCother",
    }
    allowlisted.write_metadata(borrowed)
    (allowlisted.work_dir / "transcript.json").write_text(
        json.dumps(make_transcript(borrowed)), encoding="utf-8"
    )
    allowlisted.edit_highlights(lambda record: record.update(source=borrowed))

    assert "different channel" in refused(allowlisted)
    assert tools.ffmpeg_calls == []


def test_provenance_is_compared_with_the_source_at_the_moment_of_cutting(
    project, tools, monkeypatch
):
    record = json.loads(project.highlights_path.read_text(encoding="utf-8"))
    record["source"] = {**UNVERIFIED, "rights_basis": "own"}
    monkeypatch.setattr(cut, "load_highlights", lambda source_id: (project.work_dir, record))

    assert "provenance" in refused(project)
    assert tools.ffmpeg_calls == []


@pytest.mark.parametrize(
    "change",
    [
        lambda h: h.update(duration_seconds=12.0),
        lambda h: h.pop("duration_seconds"),
        lambda h: h.update(end=5000.0),
        lambda h: h.update(start=-5.0),
        lambda h: h.update(end=h["start"] + 300),
        lambda h: h.update(end=h["start"] + 2),
        lambda h: h.update(start="0"),
        lambda h: h.update(rank=2),
        lambda h: h.pop("hook_title"),
    ],
)
def test_highlights_edited_past_the_recorded_limits_are_refused(project, tools, change):
    project.edit_highlights(lambda record: change(record["highlights"][0]))

    refused(project)
    assert tools.ffmpeg_calls == []


# --- cutting never publishes ------------------------------------------------


def test_a_clip_mode_job_is_not_publishable_just_because_it_was_cut(project):
    result = cut_highlights(SOURCE_ID, [1])
    job_id = result["staged"][0]["job_id"]
    manifest = project.manifest(job_id)

    assert manifest["format_compliance"]["passed"] is True
    assert "permission_ledger_reference" not in manifest
    with pytest.raises(YouTubePublishError):
        _check_eligibility(job_id, manifest)


def test_the_cut_module_has_no_path_to_publishing():
    source = Path(cut.__file__).read_text(encoding="utf-8")

    assert "pipeline.publish" not in source
    assert "upload_video" not in source


# --- command line -----------------------------------------------------------


def test_command_line_cuts_selected_highlights_and_says_where(project, capsys):
    assert cut.main([SOURCE_ID, "--only", "1,3"]) == 0

    out = capsys.readouterr().out
    assert "Cut 2 clip(s)" in out
    for job_id in project.jobs():
        assert f"staged/{job_id}/output.mp4" in out
    assert "unverified" in out
    assert "Nothing is published" in out


def test_command_line_reports_a_refusal(project, capsys):
    assert cut.main([SOURCE_ID, "--only", "9"]) == 1
    assert "cut failed" in capsys.readouterr().err


def test_command_line_rejects_a_malformed_only_list(project):
    with pytest.raises(SystemExit):
        cut.main([SOURCE_ID, "--only", "1,x"])
