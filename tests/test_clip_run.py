"""The clip-mode command line: every stage must hand over the exact next command.

The walk-through below runs each printed command for real, with yt-dlp,
ffmpeg, ffprobe and Whisper replaced by stand-ins. Nothing is downloaded,
decoded or published.
"""

import json
import re
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.clip import intake, transcribe as transcribe_module
from pipeline.format import validate as format_validate
from pipeline.orchestrate import run_clip

README = Path(__file__).resolve().parents[1] / "README.md"
SOURCE_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")
NEXT_COMMAND = re.compile(r"-m pipeline\.orchestrate\.run_clip (.+)$", re.MULTILINE)

PROBE = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
    "format": {"duration": "120.0"},
}
PICKS = [
    {"start": 10, "end": 40, "hook_title": "First moment", "score": 80, "rationale": "Stands alone."},
    {"start": 50, "end": 80, "hook_title": "Second moment", "score": 60, "rationale": "Also works."},
]


def fake_tool(cmd, **kwargs):
    if cmd[0] == "ffprobe":
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(PROBE), stderr="")
    assert cmd[0] == "ffmpeg", f"unexpected tool: {cmd[0]}"
    Path(cmd[-1]).write_bytes(b"a finished clip")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


class FakeWhisper:
    """Two minutes of speech: a 10-second line every 10 seconds, five words each."""

    def transcribe(self, audio, **options):
        segments = [
            SimpleNamespace(
                start=float(start),
                end=float(start + 10),
                text=" some words",
                words=[
                    SimpleNamespace(start=float(t), end=float(t + 2), word=f" w{t}")
                    for t in range(start, start + 10, 2)
                ],
            )
            for start in range(0, 120, 10)
        ]
        return iter(segments), SimpleNamespace(language="en", duration=120.0)


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "RAW_CLIP_DIR", tmp_path / "raw" / "clip")
    monkeypatch.setattr(transcribe_module, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(format_validate, "STAGED_DIR", tmp_path / "staged")
    monkeypatch.setattr(transcribe_module, "_load_env_file", lambda: None)
    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    monkeypatch.setattr(transcribe_module, "_decode_audio", lambda path, ffmpeg: "decoded audio")
    monkeypatch.setattr(transcribe_module, "_load_model", lambda size: FakeWhisper())
    monkeypatch.setattr(subprocess, "run", fake_tool)

    video = tmp_path / "my talk.mp4"
    video.write_bytes(b"video")
    picks = tmp_path / "picks.json"
    picks.write_text(json.dumps(PICKS), encoding="utf-8")
    return SimpleNamespace(root=tmp_path, video=video, picks=picks, staged=tmp_path / "staged")


@pytest.fixture
def run(capsys):
    def run_command(*argv):
        code = run_clip.main([str(part) for part in argv])
        captured = capsys.readouterr()
        return SimpleNamespace(
            code=code,
            out=captured.out,
            err=captured.err,
            next=[shlex.split(line) for line in NEXT_COMMAND.findall(captured.out)],
        )

    return run_command


def take_in(run, project, *flags):
    result = run("intake-file", project.video, *flags)
    assert result.code == 0, result.err
    return SOURCE_ID.search(result.out).group(0)


def staged_jobs(project):
    return sorted(path.name for path in project.staged.iterdir()) if project.staged.is_dir() else []


# --- the chain --------------------------------------------------------------


def test_each_stage_prints_a_next_command_that_runs(run, project):
    """Follow only what the tool prints, from a video file to staged clips."""
    result = run("intake-file", project.video)
    source_id = SOURCE_ID.search(result.out).group(0)
    assert result.next == [["transcribe", source_id]]

    result = run(*result.next[0])
    assert result.code == 0, result.err
    assert result.next == [["find-highlights", source_id]]

    result = run(*result.next[0])
    assert result.code == 0, result.err
    assert "highlight-finder" in result.out
    assert result.next == [["record-highlights", source_id, "<picks.json>"]]

    result = run(*result.next[0][:-1], project.picks)
    assert result.code == 0, result.err
    assert "First moment" in result.out and "Second moment" in result.out
    assert result.next == [["cut", source_id], ["cut", source_id, "--only", "1"]]

    # Both offered commands have to work: the example first, then the rest.
    result = run(*result.next[1])
    assert result.code == 0, result.err
    assert len(staged_jobs(project)) == 1

    result = run("cut", source_id)
    assert result.code == 0, result.err
    assert "was already cut" in result.out
    assert len(staged_jobs(project)) == 2
    for job_id in staged_jobs(project):
        assert f"staged/{job_id}" in result.out
    assert "Nothing is published" in result.out
    assert result.next == [["status", source_id]]

    result = run(*result.next[0])
    assert result.code == 0, result.err
    assert "clips cut (2 of 2)" in result.out
    assert "Nothing left to run" in result.out
    assert result.next == []


def test_status_always_names_the_stage_that_comes_next(run, project):
    source_id = take_in(run, project)

    result = run("status", source_id)
    assert "[ ] transcribed" in result.out
    assert result.next == [["transcribe", source_id]]

    run("transcribe", source_id)
    result = run("status", source_id)
    assert "[x] transcribed" in result.out and "[ ] highlights recorded" in result.out
    assert result.next == [["find-highlights", source_id]]

    run("record-highlights", source_id, project.picks)
    result = run("status", source_id)
    assert "clips cut (0 of 2)" in result.out
    assert result.out.count("not cut yet") == 2
    assert result.next == [["cut", source_id]]

    run("cut", source_id, "--only", "2")
    result = run("status", source_id)
    assert "clips cut (1 of 2)" in result.out
    assert result.out.count("not cut yet") == 1
    assert f"staged/{staged_jobs(project)[0]}/output.mp4" in result.out
    assert "format (shorts): pass" in result.out
    assert result.next == [["cut", source_id]]


def test_status_mentions_clips_left_over_from_earlier_picks(run, project):
    source_id = take_in(run, project)
    run("transcribe", source_id)
    run("record-highlights", source_id, project.picks)
    run("cut", source_id)
    project.picks.write_text(
        json.dumps([{**PICKS[0], "start": 20, "end": 50}]), encoding="utf-8"
    )
    run("record-highlights", source_id, project.picks)

    result = run("status", source_id)

    assert "clips cut (0 of 1)" in result.out
    assert "2 more staged clip(s)" in result.out


def test_next_commands_name_an_interpreter_that_can_be_pasted(run, project, monkeypatch):
    source_id = take_in(run, project)
    venv_python = project.root / "my venv" / "bin" / "python"

    monkeypatch.chdir(project.root)
    monkeypatch.setattr(run_clip.sys, "executable", str(venv_python))
    inside = run("status", source_id).out
    monkeypatch.setattr(run_clip.sys, "executable", "/opt/tools/python3")
    outside = run("status", source_id).out

    # Relative when it lives under the folder you're in, quoted for the space.
    assert "  'my venv/bin/python' -m pipeline.orchestrate.run_clip transcribe" in inside
    # Otherwise the real interpreter, never a bare "python" that may not exist.
    assert "  /opt/tools/python3 -m pipeline.orchestrate.run_clip transcribe" in outside


def test_status_survives_a_source_with_gaps_in_its_optional_fields(run, project):
    source_id = take_in(run, project)
    run("transcribe", source_id)
    metadata_path = project.root / "raw" / "clip" / source_id / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    del metadata["duration_seconds"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    result = run("status", source_id)

    assert result.code in (0, 1)
    assert "Traceback" not in result.err


# --- single stages ----------------------------------------------------------


def test_cut_passes_the_selection_and_profile_through(run, project):
    source_id = take_in(run, project)
    run("transcribe", source_id)
    run("record-highlights", source_id, project.picks)

    result = run("cut", source_id, "--only", "2", "--profile", "reels")

    assert result.code == 0, result.err
    assert "Cut 1 clip(s)" in result.out and "Second moment" in result.out
    (job_id,) = staged_jobs(project)
    manifest = json.loads((project.staged / job_id / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["highlight"]["rank"] == 2
    assert manifest["format_compliance"]["profile"] == "reels"


def test_intake_takes_a_url_and_points_at_transcribe(run, project, monkeypatch):
    asked = []

    def fake_intake_url(url):
        asked.append(url)
        source_dir = project.root / "raw" / "clip" / "20261113T101500Z-a1b2c3"
        source_dir.mkdir(parents=True)
        (source_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "source_id": source_dir.name,
                    "title": "A long podcast",
                    "duration_seconds": 3600.0,
                    "rights_basis": "unverified",
                }
            ),
            encoding="utf-8",
        )
        return source_dir

    monkeypatch.setattr(run_clip, "intake_url", fake_intake_url)

    result = run("intake", "https://www.youtube.com/watch?v=abc")

    assert asked == ["https://www.youtube.com/watch?v=abc"]
    assert "60.0 min" in result.out
    assert result.next == [["transcribe", "20261113T101500Z-a1b2c3"]]


# --- rights_basis is said out loud, and only a local file can be `own` ------


def test_an_unverified_source_is_called_out_at_intake_cut_and_status(run, project):
    source_id = take_in(run, project)
    run("transcribe", source_id)
    run("record-highlights", source_id, project.picks)

    assert "rights_basis: unverified" in run("status", source_id).out
    for result in (run("cut", source_id), run("status", source_id)):
        assert "nobody has checked that this source may be reused" in result.out


def test_own_footage_is_recorded_as_own_and_not_called_unverified(run, project):
    source_id = take_in(run, project, "--own")

    result = run("status", source_id)

    assert "rights_basis: own" in result.out
    assert "nobody has checked" not in result.out


def test_a_url_cannot_be_marked_as_own_footage(run):
    with pytest.raises(SystemExit):
        run("intake", "https://www.youtube.com/watch?v=abc", "--own")


# --- refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["intake", "not a url"],
        ["intake-file", "/no/such/video.mp4"],
        ["transcribe", "20261113T101500Z-a1b2c3"],
        ["find-highlights", "20261113T101500Z-a1b2c3"],
        ["record-highlights", "20261113T101500Z-a1b2c3", "picks.json"],
        ["cut", "20261113T101500Z-a1b2c3"],
        ["status", "20261113T101500Z-a1b2c3"],
        ["status", "../../etc"],
    ],
)
def test_a_stage_that_cannot_run_says_why_and_exits_nonzero(run, argv):
    result = run(*argv)

    assert result.code == 1
    assert result.err.startswith("error: ")
    assert result.next == []


def test_rejected_picks_are_reported_and_nothing_is_recorded(run, project):
    source_id = take_in(run, project)
    run("transcribe", source_id)
    project.picks.write_text(json.dumps([{**PICKS[0], "end": 500}]), encoding="utf-8")

    result = run("record-highlights", source_id, project.picks)

    assert result.code == 1
    assert "highlights rejected" in result.err
    assert run("status", source_id).next == [["find-highlights", source_id]]


# --- clip mode ends at staged clips -----------------------------------------


def test_there_is_no_way_to_publish_from_this_command():
    subcommands = run_clip.build_parser()._subparsers._group_actions[0].choices
    source = Path(run_clip.__file__).read_text(encoding="utf-8")

    assert set(subcommands) == {
        "intake",
        "intake-file",
        "transcribe",
        "find-highlights",
        "record-highlights",
        "cut",
        "status",
    }
    assert "pipeline.publish" not in source
    assert "upload_video" not in source


def test_readme_shows_every_clip_mode_stage():
    readme = README.read_text(encoding="utf-8")
    subcommands = run_clip.build_parser()._subparsers._group_actions[0].choices

    for name in subcommands:
        assert f"run_clip {name} " in readme, f"README no longer shows `run_clip {name}`"
