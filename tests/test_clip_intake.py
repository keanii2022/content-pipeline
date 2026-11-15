"""Clip-mode intake must record where a source came from and never
overstate the right to reuse it.

yt-dlp and ffprobe are replaced with stand-ins, so nothing is downloaded
and no real video is needed.
"""

import datetime
import json
import subprocess

import pytest

from pipeline.clip import intake
from pipeline.permissions import validate

NASA_VIDEO = {
    "webpage_url": "https://www.youtube.com/watch?v=abc",
    "title": "Artemis flyby",
    "uploader": "NASA",
    "uploader_url": "https://www.youtube.com/@NASA",
    "channel_id": "UCLA_DiR1FfKNvjuUpBHmylQ",
    "channel_url": "https://www.youtube.com/channel/UCLA_DiR1FfKNvjuUpBHmylQ",
    "license": "Creative Commons Attribution license (reuse allowed)",
    "duration": 1200,
}

LEDGER = """\
creators:
  - creator_id: nasa
    display_name: "NASA"
    platform: youtube
    source_url: "https://www.youtube.com/@NASA"
    permission_scope: "public domain"
    permission_granted_date: "2026-09-13"
    evidence: "https://www.nasa.gov/"
"""


class FakeTools:
    """Stands in for subprocess.run: answers yt-dlp and ffprobe calls."""

    def __init__(self):
        self.video_info = dict(NASA_VIDEO)
        self.duration = 1200.0
        self.has_audio = True
        self.download_fails = False
        self.interrupt_download = False
        self.downloaded_info = None
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[0] == "ffprobe":
            streams = [{"codec_type": "video"}]
            if self.has_audio:
                streams.append({"codec_type": "audio"})
            probe = {"format": {"duration": str(self.duration)}, "streams": streams}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(probe), stderr="")
        if "--dump-single-json" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(self.video_info), stderr=""
            )
        if self.interrupt_download:
            raise KeyboardInterrupt()
        if self.download_fails:
            raise subprocess.CalledProcessError(1, cmd, stderr="HTTP Error 403")
        with open(cmd[cmd.index("--load-info-json") + 1], encoding="utf-8") as f:
            self.downloaded_info = json.load(f)
        template = cmd[cmd.index("-o") + 1]
        with open(template.replace("%(ext)s", "mp4"), "wb") as f:
            f.write(b"video")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    @property
    def downloads(self):
        return [c for c in self.calls if c[0] == "yt-dlp" and "--dump-single-json" not in c]


@pytest.fixture
def tools(monkeypatch):
    fake = FakeTools()
    monkeypatch.setattr(intake.subprocess, "run", fake)
    return fake


@pytest.fixture
def raw_clip_dir(tmp_path, monkeypatch):
    target = tmp_path / "raw" / "clip"
    monkeypatch.setattr(intake, "RAW_CLIP_DIR", target)
    return target


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "allowlist.yaml"
    path.write_text(LEDGER, encoding="utf-8")
    # load_ledger's default argument was bound at import, so patch that too.
    monkeypatch.setattr(validate, "ALLOWLIST_PATH", path)
    monkeypatch.setattr(validate.load_ledger, "__defaults__", (path,))
    return path


@pytest.fixture
def local_video(tmp_path):
    path = tmp_path / "My Podcast Ep 4.MOV"
    path.write_bytes(b"video")
    return path


def read_metadata(source_dir):
    return json.loads((source_dir / "metadata.json").read_text(encoding="utf-8"))


# --- rights_basis: the part that must never be wrong -----------------------


def test_url_from_allowlisted_channel_is_allowlist(tools, raw_clip_dir):
    metadata = read_metadata(intake.intake_url("https://www.youtube.com/watch?v=abc"))

    assert metadata["rights_basis"] == "allowlist"
    assert metadata["permission_ledger_reference"]["creator_id"] == "nasa"


def test_allowlist_match_ignores_handle_case_and_www(tools, raw_clip_dir):
    tools.video_info["uploader_url"] = "http://youtube.com/@nasa/"

    metadata = read_metadata(intake.intake_url("https://youtu.be/abc"))

    assert metadata["rights_basis"] == "allowlist"


def test_url_from_unknown_channel_is_unverified(tools, raw_clip_dir):
    tools.video_info.update(
        uploader="Some Podcast",
        uploader_url="https://www.youtube.com/@somepodcast",
        channel_url="https://www.youtube.com/channel/UCxxxx",
    )

    metadata = read_metadata(intake.intake_url("https://www.youtube.com/watch?v=abc"))

    assert metadata["rights_basis"] == "unverified"
    assert metadata["permission_ledger_reference"] is None


def test_channel_merely_named_like_an_allowlisted_creator_is_unverified(tools, raw_clip_dir):
    tools.video_info.update(
        uploader="NASA",
        uploader_url="https://www.youtube.com/@NASA-reuploads",
        channel_url="https://www.youtube.com/channel/UCfake",
    )

    metadata = read_metadata(intake.intake_url("https://www.youtube.com/watch?v=abc"))

    assert metadata["rights_basis"] == "unverified"


def test_pasted_url_mentioning_an_allowlisted_channel_is_unverified(tools, raw_clip_dir):
    tools.video_info.update(
        uploader_url="https://www.youtube.com/@somepodcast",
        channel_url="https://www.youtube.com/channel/UCxxxx",
    )

    metadata = read_metadata(
        intake.intake_url("https://evil.example/watch?from=https://www.youtube.com/@NASA")
    )

    assert metadata["rights_basis"] == "unverified"


def test_same_handle_on_another_site_is_unverified(tools, raw_clip_dir):
    tools.video_info.update(
        uploader_url="https://www.tiktok.com/@NASA",
        channel_url=None,
    )

    metadata = read_metadata(intake.intake_url("https://www.tiktok.com/@NASA/video/1"))

    assert metadata["rights_basis"] == "unverified"


def test_video_hosted_elsewhere_claiming_an_allowlisted_channel_is_unverified(
    tools, raw_clip_dir
):
    # A page on another site can put any channel URL in its own metadata.
    tools.video_info["webpage_url"] = "https://evil.example/v"

    metadata = read_metadata(intake.intake_url("https://evil.example/v"))

    assert metadata["rights_basis"] == "unverified"


@pytest.mark.parametrize(
    "webpage_url",
    ["https://youtube.com@evil.example/watch?v=abc", "https://youtube.com.evil.example/v", None],
)
def test_lookalike_or_missing_video_host_is_unverified(tools, raw_clip_dir, webpage_url):
    tools.video_info["webpage_url"] = webpage_url

    metadata = read_metadata(intake.intake_url("https://www.youtube.com/watch?v=abc"))

    assert metadata["rights_basis"] == "unverified"


def test_channels_told_apart_only_by_query_string_never_match(tools, raw_clip_dir, ledger):
    ledger.write_text(
        LEDGER.replace("https://www.youtube.com/@NASA", "https://example.tv/user?id=alice"),
        encoding="utf-8",
    )
    tools.video_info.update(
        webpage_url="https://example.tv/watch/1",
        uploader_url="https://example.tv/user?id=mallory",
        channel_url=None,
    )

    metadata = read_metadata(intake.intake_url("https://example.tv/watch/1"))

    assert metadata["rights_basis"] == "unverified"


def test_handle_case_only_folds_on_youtube(tools, raw_clip_dir, ledger):
    ledger.write_text(
        LEDGER.replace("https://www.youtube.com/@NASA", "https://example.tv/@Alice"),
        encoding="utf-8",
    )
    tools.video_info.update(
        webpage_url="https://example.tv/watch/1",
        uploader_url="https://example.tv/@alice",
        channel_url=None,
    )

    metadata = read_metadata(intake.intake_url("https://example.tv/watch/1"))

    assert metadata["rights_basis"] == "unverified"


def test_download_uses_the_info_rights_were_judged_on_not_the_url_again(tools, raw_clip_dir):
    url = "https://short.example/x"

    metadata = read_metadata(intake.intake_url(url))

    assert metadata["rights_basis"] == "allowlist"
    assert len(tools.downloads) == 1
    assert url not in tools.downloads[0]
    assert tools.downloaded_info == tools.video_info
    assert sorted(p.name for p in (raw_clip_dir / metadata["source_id"]).iterdir()) == [
        "metadata.json",
        "source.mp4",
    ]


def test_channel_id_match_is_case_sensitive(tools, raw_clip_dir, ledger):
    ledger.write_text(
        LEDGER.replace(
            "https://www.youtube.com/@NASA", "https://www.youtube.com/channel/UCabcDEF"
        ),
        encoding="utf-8",
    )
    tools.video_info.update(
        uploader_url=None, channel_url="https://www.youtube.com/channel/UCABCdef"
    )
    assert intake._comparable_channel_url(
        "https://www.youtube.com/channel/UCabcDEF"
    ) == "youtube.com/channel/UCabcDEF"

    metadata = read_metadata(intake.intake_url("https://www.youtube.com/watch?v=abc"))

    assert metadata["rights_basis"] == "unverified"


def test_expired_allowlist_entry_is_unverified(tools, raw_clip_dir, ledger):
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    ledger.write_text(LEDGER + f'    expiry_date: "{yesterday.isoformat()}"\n', encoding="utf-8")

    metadata = read_metadata(intake.intake_url("https://www.youtube.com/watch?v=abc"))

    assert metadata["rights_basis"] == "unverified"


def test_video_with_no_reported_channel_is_unverified(tools, raw_clip_dir):
    tools.video_info = {"title": "direct file", "duration": 60}

    metadata = read_metadata(intake.intake_url("https://example.com/video.mp4"))

    assert metadata["rights_basis"] == "unverified"
    assert metadata["uploader"] is None
    assert metadata["license"] is None


def test_broken_ledger_stops_intake_before_downloading(tools, raw_clip_dir, ledger):
    ledger.write_text("creators: not-a-list\n", encoding="utf-8")

    with pytest.raises(intake.IntakeError, match="ledger"):
        intake.intake_url("https://www.youtube.com/watch?v=abc")

    assert tools.downloads == []


def test_local_file_is_unverified_unless_marked_own(tools, raw_clip_dir, local_video):
    assert read_metadata(intake.intake_file(local_video))["rights_basis"] == "unverified"
    assert read_metadata(intake.intake_file(local_video, own=True))["rights_basis"] == "own"


@pytest.mark.parametrize("not_a_bool", ["false", "yes", 1, None])
def test_own_must_be_a_real_boolean(tools, raw_clip_dir, local_video, not_a_bool):
    with pytest.raises(intake.IntakeError, match="own"):
        intake.intake_file(local_video, own=not_a_bool)

    assert not raw_clip_dir.exists()


def test_cli_refuses_own_on_a_url(tools, raw_clip_dir, capsys):
    assert intake.main(["https://www.youtube.com/watch?v=abc", "--own"]) == 1

    assert "--own" in capsys.readouterr().err
    assert tools.calls == []


# --- provenance is written down --------------------------------------------


def test_url_metadata_records_provenance(tools, raw_clip_dir):
    url = "https://www.youtube.com/watch?v=abc"
    source_dir = intake.intake_url(url)
    metadata = read_metadata(source_dir)

    assert source_dir.parent == raw_clip_dir
    assert metadata["source_id"] == source_dir.name
    assert intake._SAFE_PATH_COMPONENT.match(metadata["source_id"])
    assert intake._SOURCE_ID.match(metadata["source_id"])
    assert metadata["source_url"] == url
    assert metadata["original_filename"] is None
    assert metadata["title"] == "Artemis flyby"
    assert metadata["uploader"] == "NASA"
    assert metadata["channel_id"] == "UCLA_DiR1FfKNvjuUpBHmylQ"
    assert metadata["license"] == NASA_VIDEO["license"]
    assert metadata["duration_seconds"] == 1200.0
    assert metadata["fetched_at"]
    assert (source_dir / metadata["source_file"]).exists()


def test_file_metadata_records_provenance(tools, raw_clip_dir, local_video):
    source_dir = intake.intake_file(local_video, own=True)
    metadata = read_metadata(source_dir)

    assert metadata["original_filename"] == "My Podcast Ep 4.MOV"
    assert metadata["source_url"] is None
    assert metadata["title"] == "My Podcast Ep 4"
    assert metadata["uploader"] is None
    assert metadata["license"] is None
    assert metadata["source_file"] == "source.mov"
    assert (source_dir / "source.mov").read_bytes() == b"video"
    assert local_video.exists()


# --- rejections ------------------------------------------------------------


def test_over_three_hours_is_rejected_before_downloading(tools, raw_clip_dir):
    tools.video_info["duration"] = 3 * 3600 + 1

    with pytest.raises(intake.IntakeError, match="3 hours"):
        intake.intake_url("https://www.youtube.com/watch?v=abc")

    assert tools.downloads == []
    assert not raw_clip_dir.exists()


def test_exactly_three_hours_is_accepted(tools, raw_clip_dir):
    tools.video_info["duration"] = 3 * 3600
    tools.duration = 3 * 3600.0

    assert intake.intake_url("https://www.youtube.com/watch?v=abc").exists()


def test_long_download_with_no_reported_duration_is_rejected_and_removed(tools, raw_clip_dir):
    del tools.video_info["duration"]
    tools.duration = 4 * 3600.0

    with pytest.raises(intake.IntakeError, match="3 hours"):
        intake.intake_url("https://www.youtube.com/watch?v=abc")

    assert list(raw_clip_dir.iterdir()) == []


def test_download_without_audio_is_rejected_and_removed(tools, raw_clip_dir):
    tools.has_audio = False

    with pytest.raises(intake.IntakeError, match="no audio"):
        intake.intake_url("https://www.youtube.com/watch?v=abc")

    assert list(raw_clip_dir.iterdir()) == []


def test_failed_download_leaves_nothing_behind(tools, raw_clip_dir):
    tools.download_fails = True

    with pytest.raises(intake.IntakeError, match="403"):
        intake.intake_url("https://www.youtube.com/watch?v=abc")

    assert list(raw_clip_dir.iterdir()) == []


def test_interrupted_download_leaves_nothing_behind(tools, raw_clip_dir):
    tools.interrupt_download = True

    with pytest.raises(KeyboardInterrupt):
        intake.intake_url("https://www.youtube.com/watch?v=abc")

    assert list(raw_clip_dir.iterdir()) == []


@pytest.mark.parametrize("take_in", ["url", "file"])
def test_source_is_removed_if_metadata_cannot_be_written(
    tools, raw_clip_dir, local_video, monkeypatch, take_in
):
    def disk_full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(intake, "_write_metadata", disk_full)

    with pytest.raises(OSError):
        if take_in == "url":
            intake.intake_url("https://www.youtube.com/watch?v=abc")
        else:
            intake.intake_file(local_video)

    assert list(raw_clip_dir.iterdir()) == []


def test_local_file_over_three_hours_or_silent_is_not_copied(tools, raw_clip_dir, local_video):
    tools.duration = 3 * 3600 + 1.0
    with pytest.raises(intake.IntakeError, match="3 hours"):
        intake.intake_file(local_video)

    tools.duration = 60.0
    tools.has_audio = False
    with pytest.raises(intake.IntakeError, match="no audio"):
        intake.intake_file(local_video)

    assert not raw_clip_dir.exists()


def test_missing_local_file_is_rejected(tools, raw_clip_dir, tmp_path):
    with pytest.raises(intake.IntakeError, match="no file"):
        intake.intake_file(tmp_path / "nope.mp4")


@pytest.mark.parametrize("bad_url", ["", "not a url", "ftp://example.com/v.mp4", "--exec=rm"])
def test_non_http_urls_are_rejected_without_calling_yt_dlp(tools, raw_clip_dir, bad_url):
    with pytest.raises(intake.IntakeError, match="http"):
        intake.intake_url(bad_url)

    assert tools.calls == []


def test_playlists_and_live_streams_are_rejected(tools, raw_clip_dir):
    tools.video_info["_type"] = "playlist"
    with pytest.raises(intake.IntakeError, match="playlist"):
        intake.intake_url("https://www.youtube.com/@NASA/videos")

    tools.video_info = dict(NASA_VIDEO, is_live=True)
    with pytest.raises(intake.IntakeError, match="live"):
        intake.intake_url("https://www.youtube.com/watch?v=live")

    assert tools.downloads == []


@pytest.mark.parametrize("bad_id", ["../escape", "..", ".", "my-video", "20261113T101500Z-XYZ123"])
def test_source_id_must_match_the_timestamp_pattern(tools, raw_clip_dir, local_video, bad_id):
    with pytest.raises(intake.IntakeError, match="source_id"):
        intake.intake_file(local_video, source_id=bad_id)

    assert not raw_clip_dir.exists()


def test_local_path_reaches_ffprobe_as_an_absolute_path(tools, raw_clip_dir, tmp_path, monkeypatch):
    (tmp_path / "-report").write_bytes(b"video")
    monkeypatch.chdir(tmp_path)

    intake.intake_file("-report")

    assert tools.calls[0][-1] == str((tmp_path / "-report").resolve())


def test_yt_dlp_ignores_the_users_own_config(tools, raw_clip_dir):
    intake.intake_url("https://www.youtube.com/watch?v=abc")

    yt_dlp_calls = [c for c in tools.calls if c[0] == "yt-dlp"]
    assert len(yt_dlp_calls) == 2
    assert all("--ignore-config" in c for c in yt_dlp_calls)
