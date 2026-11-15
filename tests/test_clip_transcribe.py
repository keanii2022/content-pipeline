"""Clip-mode transcription must write word-level timings and carry the
source's provenance forward untouched.

Whisper is replaced with a stand-in model, so nothing is downloaded.
"""

import json
import subprocess
from types import SimpleNamespace

import pytest

from pipeline.clip import intake, transcribe as transcribe_module
from pipeline.clip.transcribe import TranscribeError, transcribe

SOURCE_ID = "20261113T101500Z-a1b2c3"

METADATA = {
    "source_id": SOURCE_ID,
    "source_kind": "url",
    "source_url": "https://www.youtube.com/watch?v=abc",
    "original_filename": None,
    "title": "A long podcast",
    "uploader": "Some Podcast",
    "uploader_url": "https://www.youtube.com/@somepodcast",
    "channel_id": "UCxxxx",
    "channel_url": "https://www.youtube.com/channel/UCxxxx",
    "license": None,
    "duration_seconds": 125.0,
    "source_file": "source.mp4",
    "fetched_at": "2026-11-13T10:15:00+00:00",
    "rights_basis": "unverified",
    "permission_ledger_reference": None,
}


def word(start, end, text):
    return SimpleNamespace(start=start, end=end, word=text)


def segment(start, end, text, words):
    return SimpleNamespace(start=start, end=end, text=text, words=words)


SEGMENTS = [
    segment(0.0, 2.5, " Hello there.", [word(0.0, 0.61234, " Hello"), word(0.7, 2.5, " there.")]),
    segment(2.5, 3.0, "   ", []),
    segment(125.2, 127.0, " Second point.", [word(125.2, 126.0, " Second"), word(126.0, 127.0, " point.")]),
]


class FakeWhisper:
    def __init__(self, segments=SEGMENTS, language="en", duration=130.0):
        self.segments = segments
        self.info = SimpleNamespace(language=language, duration=duration)
        self.calls = []

    def transcribe(self, path, **options):
        self.calls.append((path, options))
        return iter(self.segments), self.info


@pytest.fixture(autouse=True)
def folders(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "RAW_CLIP_DIR", tmp_path / "raw" / "clip")
    monkeypatch.setattr(transcribe_module, "WORK_DIR", tmp_path / "work")
    # Keep the real .env (and any real Whisper download) out of the tests.
    monkeypatch.setattr(transcribe_module, "_load_env_file", lambda: None)
    monkeypatch.delenv("WHISPER_MODEL", raising=False)

    monkeypatch.setattr(
        transcribe_module, "_decode_audio", lambda path, ffmpeg: ("decoded audio of", path)
    )

    def no_real_model(size):
        raise AssertionError("tests must not load a real Whisper model")

    monkeypatch.setattr(transcribe_module, "_load_model", no_real_model)
    return tmp_path


def make_source(folders, **overrides):
    source_dir = folders / "raw" / "clip" / SOURCE_ID
    source_dir.mkdir(parents=True)
    (source_dir / "source.mp4").write_bytes(b"video")
    metadata = {**METADATA, **overrides}
    (source_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return metadata


def read_transcript(work_dir):
    return json.loads((work_dir / "transcript.json").read_text(encoding="utf-8"))


def test_writes_word_level_transcript(folders):
    make_source(folders)
    model = FakeWhisper()

    work_dir = transcribe(SOURCE_ID, model=model)
    transcript = read_transcript(work_dir)

    assert work_dir == folders / "work" / f"clip-{SOURCE_ID}"
    assert transcript["language"] == "en"
    assert transcript["duration_seconds"] == 130.0
    assert transcript["segments"] == [
        {
            "start": 0.0,
            "end": 2.5,
            "text": "Hello there.",
            "words": [
                {"start": 0.0, "end": 0.612, "word": "Hello"},
                {"start": 0.7, "end": 2.5, "word": "there."},
            ],
        },
        {
            "start": 125.2,
            "end": 127.0,
            "text": "Second point.",
            "words": [
                {"start": 125.2, "end": 126.0, "word": "Second"},
                {"start": 126.0, "end": 127.0, "word": "point."},
            ],
        },
    ]
    audio, options = model.calls[0]
    assert audio == ("decoded audio of", folders / "raw" / "clip" / SOURCE_ID / "source.mp4")
    assert options["word_timestamps"] is True


def test_text_file_has_one_timestamped_line_per_segment(folders):
    make_source(folders)

    work_dir = transcribe(SOURCE_ID, model=FakeWhisper())

    assert (work_dir / "transcript.txt").read_text(encoding="utf-8") == (
        "[00:00] Hello there.\n[02:05] Second point.\n"
    )


def test_minutes_keep_counting_past_an_hour():
    assert transcribe_module._timestamp(2 * 3600 + 5 * 60 + 7.9) == "[125:07]"


# --- provenance ------------------------------------------------------------


LEDGER_REFERENCE = {
    "creator_id": "nasa",
    "source_url": "https://www.youtube.com/@NASA",
    "permission_granted_date": "2026-09-13",
}
OWN_FILE = {"source_kind": "file", "source_url": None, "original_filename": "mine.mov"}

CONSISTENT_SOURCES = {
    "unverified": {},
    "allowlist": {"permission_ledger_reference": LEDGER_REFERENCE},
    "own": OWN_FILE,
}


@pytest.mark.parametrize("rights_basis", ["allowlist", "own", "unverified"])
def test_provenance_is_copied_forward_unchanged(folders, rights_basis):
    metadata = make_source(
        folders, rights_basis=rights_basis, **CONSISTENT_SOURCES[rights_basis]
    )

    transcript = read_transcript(transcribe(SOURCE_ID, model=FakeWhisper()))

    assert transcript["source_id"] == SOURCE_ID
    assert transcript["source"] == {f: metadata[f] for f in intake.PROVENANCE_FIELDS}
    assert transcript["source"]["rights_basis"] == rights_basis


@pytest.mark.parametrize("field", intake.PROVENANCE_FIELDS)
def test_source_with_a_missing_provenance_field_is_refused(folders, field):
    metadata = make_source(folders)
    del metadata[field]
    (folders / "raw" / "clip" / SOURCE_ID / "metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )

    with pytest.raises(TranscribeError, match=field):
        transcribe(SOURCE_ID, model=FakeWhisper())

    assert not (folders / "work").exists()


@pytest.mark.parametrize("bad_basis", ["", None, "Own", "licensed", "verified"])
def test_source_with_an_unknown_rights_basis_is_refused(folders, bad_basis):
    make_source(folders, rights_basis=bad_basis)

    with pytest.raises(TranscribeError, match="rights_basis"):
        transcribe(SOURCE_ID, model=FakeWhisper())


@pytest.mark.parametrize(
    "overrides, complaint",
    [
        # 'own' on something downloaded from a URL
        ({"rights_basis": "own"}, "own"),
        # 'allowlist' with no ledger entry behind it
        ({"rights_basis": "allowlist"}, "permission_ledger_reference"),
        ({"rights_basis": "allowlist", "permission_ledger_reference": {}}, "permission_ledger_reference"),
        ({"rights_basis": "allowlist", "permission_ledger_reference": "nasa"}, "permission_ledger_reference"),
        # 'allowlist' on a local file, which carries no channel to match
        ({**OWN_FILE, "rights_basis": "allowlist", "permission_ledger_reference": LEDGER_REFERENCE},
         "permission_ledger_reference"),
        # nothing saying where it came from
        ({"source_url": None}, "source_url"),
        ({"source_url": "  "}, "source_url"),
        ({**OWN_FILE, "original_filename": None}, "original_filename"),
        ({"source_kind": "stream"}, "source_kind"),
        ({"source_kind": None}, "source_kind"),
    ],
)
def test_provenance_that_contradicts_itself_is_refused(folders, overrides, complaint):
    make_source(folders, **overrides)

    with pytest.raises(TranscribeError) as refusal:
        transcribe(SOURCE_ID, model=FakeWhisper())

    # Matched on the message's tail: the tmp folder in the path is named
    # after this test and would satisfy a plain match= by accident.
    assert complaint in str(refusal.value).split("metadata.json", 1)[1]
    assert not (folders / "work").exists()


def test_everything_intake_writes_counts_as_complete_provenance(folders, monkeypatch):
    """If intake stops writing a provenance field, every later step would
    refuse its sources — catch that here instead."""
    probe = {"format": {"duration": "60"}, "streams": [{"codec_type": "audio"}]}
    monkeypatch.setattr(
        intake.subprocess,
        "run",
        lambda cmd, **kw: SimpleNamespace(stdout=json.dumps(probe), stderr=""),
    )
    video = folders / "mine.mp4"
    video.write_bytes(b"video")

    source_dir = intake.intake_file(video, own=True)
    source_path, metadata = intake.load_source(source_dir.name)

    assert source_path == source_dir / "source.mp4"
    assert intake.provenance(metadata)["rights_basis"] == "own"


# --- refusals --------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["..", "../x", "clip", "", None])
def test_bad_source_id_is_refused(folders, bad_id):
    with pytest.raises(TranscribeError, match="source_id"):
        transcribe(bad_id, model=FakeWhisper())


def test_unknown_source_is_refused(folders):
    with pytest.raises(TranscribeError, match="run intake first"):
        transcribe(SOURCE_ID, model=FakeWhisper())


def test_metadata_for_a_different_source_is_refused(folders):
    make_source(folders, source_id="20261113T101500Z-ffffff")

    with pytest.raises(TranscribeError, match="does not describe"):
        transcribe(SOURCE_ID, model=FakeWhisper())


@pytest.mark.parametrize(
    "source_file", ["../../elsewhere.mp4", "/etc/hosts", "sub/source.mp4", "", ".", "..", None]
)
def test_source_file_outside_the_source_folder_is_refused(folders, source_file):
    make_source(folders, source_file=source_file)

    with pytest.raises(TranscribeError, match="invalid 'source_file' value"):
        transcribe(SOURCE_ID, model=FakeWhisper())


def test_missing_video_is_refused(folders):
    make_source(folders, source_file="gone.mp4")

    with pytest.raises(TranscribeError, match="missing"):
        transcribe(SOURCE_ID, model=FakeWhisper())


def test_silent_source_writes_nothing(folders):
    make_source(folders)

    with pytest.raises(TranscribeError, match="no speech"):
        transcribe(SOURCE_ID, model=FakeWhisper(segments=[segment(0.0, 1.0, "  ", [])]))

    assert not (folders / "work").exists()


def test_whisper_crash_keeps_the_previous_transcript(folders):
    make_source(folders)
    work_dir = transcribe(SOURCE_ID, model=FakeWhisper())
    before = (work_dir / "transcript.json").read_text(encoding="utf-8")

    def explode():
        yield SEGMENTS[0]
        raise RuntimeError("decoder died")

    broken = FakeWhisper()
    broken.transcribe = lambda path, **options: (explode(), broken.info)
    with pytest.raises(TranscribeError, match="decoder died"):
        transcribe(SOURCE_ID, model=broken)

    assert (work_dir / "transcript.json").read_text(encoding="utf-8") == before
    assert sorted(p.name for p in work_dir.iterdir()) == ["transcript.json", "transcript.txt"]


def test_failed_write_keeps_the_old_json_and_text_as_a_pair(folders, monkeypatch):
    make_source(folders)
    work_dir = transcribe(SOURCE_ID, model=FakeWhisper())
    before = {p.name: p.read_text(encoding="utf-8") for p in work_dir.iterdir()}

    real_write_text = type(work_dir).write_text

    def disk_fills_on_the_text_file(self, *args, **kwargs):
        if self.name.startswith("transcript.txt"):
            raise OSError("disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(type(work_dir), "write_text", disk_fills_on_the_text_file)
    rerun = FakeWhisper(segments=[segment(1.0, 2.0, " Different words.", [])])
    with pytest.raises(OSError):
        transcribe(SOURCE_ID, model=rerun)
    monkeypatch.undo()

    assert {p.name: p.read_text(encoding="utf-8") for p in work_dir.iterdir()} == before


def test_word_without_timing_is_skipped_not_fatal(folders):
    make_source(folders)
    untimed = segment(
        0.0, 2.0, " Hi there.",
        [word(None, 0.5, " Hi"), word(0.5, None, " there."), word(1.0, 2.0, None), word(1.0, 2.0, " ok")],
    )

    transcript = read_transcript(transcribe(SOURCE_ID, model=FakeWhisper(segments=[untimed])))

    assert transcript["segments"][0]["text"] == "Hi there."
    assert transcript["segments"][0]["words"] == [{"start": 1.0, "end": 2.0, "word": "ok"}]


# --- model choice ----------------------------------------------------------


def test_model_size_defaults_to_small_and_reads_the_setting(folders, monkeypatch):
    make_source(folders)
    assert read_transcript(transcribe(SOURCE_ID, model=FakeWhisper()))["whisper_model"] == "small"

    monkeypatch.setenv("WHISPER_MODEL", "medium")
    assert read_transcript(transcribe(SOURCE_ID, model=FakeWhisper()))["whisper_model"] == "medium"

    assert (
        read_transcript(transcribe(SOURCE_ID, model=FakeWhisper(), model_size="tiny"))[
            "whisper_model"
        ]
        == "tiny"
    )


@pytest.mark.parametrize("bad_size", ["huge", "someone/their-model", "/tmp/model", "../x"])
def test_model_setting_cannot_point_at_an_arbitrary_model(folders, monkeypatch, bad_size):
    make_source(folders)
    monkeypatch.setenv("WHISPER_MODEL", bad_size)

    with pytest.raises(TranscribeError, match="not one of"):
        transcribe(SOURCE_ID)


def test_missing_faster_whisper_says_how_to_install_it(folders, monkeypatch):
    make_source(folders)
    monkeypatch.undo()
    monkeypatch.setattr(intake, "RAW_CLIP_DIR", folders / "raw" / "clip")
    monkeypatch.setattr(transcribe_module, "WORK_DIR", folders / "work")
    monkeypatch.setattr(transcribe_module, "_decode_audio", lambda path, ffmpeg: None)
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", None)

    with pytest.raises(TranscribeError, match=r"pip install -e"):
        transcribe(SOURCE_ID, model_size="small")


def test_audio_is_decoded_by_ffmpeg_as_16k_mono_floats(folders, monkeypatch):
    numpy = pytest.importorskip("numpy")
    monkeypatch.undo()
    samples = numpy.array([0.0, 0.5, -0.5], dtype=numpy.float32)
    calls = []

    def fake_ffmpeg(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=samples.tobytes(), stderr=b"")

    monkeypatch.setattr(transcribe_module.subprocess, "run", fake_ffmpeg)

    audio = transcribe_module._decode_audio(folders / "source.mp4", "ffmpeg")

    assert audio.dtype == numpy.float32
    assert audio.tolist() == [0.0, 0.5, -0.5]
    cmd = calls[0]
    assert cmd[cmd.index("-ar") + 1] == "16000"
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-i") + 1] == str(folders / "source.mp4")


def test_unreadable_audio_is_reported(folders, monkeypatch):
    pytest.importorskip("numpy")
    monkeypatch.undo()

    def failing_ffmpeg(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr=b"Invalid data found")

    monkeypatch.setattr(transcribe_module.subprocess, "run", failing_ffmpeg)

    with pytest.raises(TranscribeError, match="Invalid data"):
        transcribe_module._decode_audio(folders / "source.mp4", "ffmpeg")


def test_progress_is_reported_as_a_fraction(folders):
    make_source(folders)
    seen = []

    transcribe(SOURCE_ID, model=FakeWhisper(), on_progress=seen.append)

    assert seen == sorted(seen)
    assert 0 < seen[0] < 1
    assert seen[-1] <= 1.0
