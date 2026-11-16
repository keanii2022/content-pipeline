"""The publish gate for clip-mode jobs.

A cut clip can go to YouTube only if its recorded rights allow it:
`allowlist` is checked against the ledger again, `own` goes through, and
`unverified` needs the person publishing to say so for that one upload.
YouTube is a stand-in throughout — nothing is uploaded.
"""

import datetime
import json
import re
import shutil
import sys
import types
from pathlib import Path

import pytest

from pipeline.clip.cut import cut_highlights
from pipeline.control import api
from pipeline.orchestrate import run_clip, run_single
from pipeline.permissions.validate import LedgerError
from pipeline.publish import youtube
from pipeline.publish.youtube import (
    ACKNOWLEDGMENT_FIELD,
    UNVERIFIED_ACK_FLAG,
    UNVERIFIED_ACK_LABEL,
    YouTubePublishError,
    upload_video,
)
from test_clip_cut import ALLOWLISTED, SOURCE_ID, UNVERIFIED, Project, dirs, tools  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
APP_JS = REPO / "review-app" / "public" / "app.js"

OWN = {
    **UNVERIFIED,
    "source_kind": "file",
    "source_url": None,
    "original_filename": "my-talk.mp4",
    "uploader": None,
    "uploader_url": None,
    "channel_id": None,
    "channel_url": None,
    "rights_basis": "own",
}


class FakeYouTube:
    """Stands in for the YouTube client and remembers what the manifest
    said at the moment the upload began."""

    def __init__(self, staged):
        self.staged = staged
        self.uploads = []
        self.acknowledgment_at_upload = "upload never began"

    def videos(self):
        return self

    def insert(self, **kwargs):
        self.uploads.append(kwargs)
        return self

    def next_chunk(self):
        job_dir = Path(self.uploads[-1]["media_body"]).parent
        manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        self.acknowledgment_at_upload = manifest.get(ACKNOWLEDGMENT_FIELD)
        return None, {"id": "vid123"}


@pytest.fixture
def fake_youtube(dirs, monkeypatch):
    staged = dirs / "staged"
    monkeypatch.setattr(youtube, "STAGED_DIR", staged)
    service = FakeYouTube(staged)
    monkeypatch.setattr(youtube, "_get_authenticated_service", lambda: service)
    http = types.ModuleType("googleapiclient.http")
    http.MediaFileUpload = lambda path, **kwargs: path
    monkeypatch.setitem(sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http)
    return service


@pytest.fixture
def ledger(monkeypatch):
    """The permissions ledger, as the publish step sees it. Permits by default."""
    state = {"permitted": True, "broken": False, "asked": []}

    def is_permitted(creator_id, source_url):
        state["asked"].append((creator_id, source_url))
        if state["broken"]:
            raise LedgerError("allowlist.yaml is not valid")
        return state["permitted"]

    monkeypatch.setattr(youtube, "is_permitted", is_permitted)
    return state


def staged_clip(dirs, provenance):
    """Cut one clip from a source with the given provenance."""
    project = Project(dirs, provenance)
    job_id = cut_highlights(SOURCE_ID, [1])["staged"][0]["job_id"]
    return project, job_id


def publish(job_id, **kwargs):
    return upload_video(job_id, title="A title I typed", **kwargs)


def refused(project, job_id, fake_youtube, **kwargs):
    """The upload is refused before anything goes out or is written down."""
    before = project.manifest(job_id)
    with pytest.raises(YouTubePublishError) as refusal:
        publish(job_id, **kwargs)
    assert fake_youtube.uploads == []
    assert project.manifest(job_id) == before
    return str(refusal.value)


def edit_manifest(project, job_id, change):
    path = project.staged / job_id / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    change(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def unverified(dirs, tools, monkeypatch, fake_youtube):
    return staged_clip(dirs, UNVERIFIED)


@pytest.fixture
def own(dirs, tools, fake_youtube):
    return staged_clip(dirs, OWN)


@pytest.fixture
def allowlisted(dirs, tools, monkeypatch, fake_youtube, ledger):
    from pipeline.clip import cut

    monkeypatch.setattr(cut, "is_permitted", lambda creator_id, source_url: True)
    return staged_clip(dirs, ALLOWLISTED)


# --- unverified: only with the person's say-so, every time ------------------


def test_an_unverified_clip_is_refused_without_the_acknowledgment(unverified, fake_youtube):
    project, job_id = unverified

    message = refused(project, job_id, fake_youtube)

    assert "unverified" in message
    assert UNVERIFIED_ACK_LABEL in message
    assert UNVERIFIED_ACK_FLAG in message


@pytest.mark.parametrize("not_a_yes", [False, None, "true", "yes", 1, [True]])
def test_only_a_real_yes_counts_as_the_acknowledgment(unverified, fake_youtube, not_a_yes):
    project, job_id = unverified

    refused(project, job_id, fake_youtube, acknowledge_unverified_rights=not_a_yes)


def test_an_unverified_clip_is_uploaded_once_acknowledged(unverified, fake_youtube):
    project, job_id = unverified

    result = publish(job_id, acknowledge_unverified_rights=True)

    assert result == {"video_id": "vid123", "url": "https://youtube.com/watch?v=vid123"}
    assert len(fake_youtube.uploads) == 1
    assert fake_youtube.uploads[0]["body"]["snippet"]["title"] == "A title I typed"
    assert fake_youtube.uploads[0]["body"]["status"]["privacyStatus"] == "private"


def test_the_acknowledgment_and_its_time_are_recorded_in_the_manifest(unverified, fake_youtube):
    project, job_id = unverified
    before = project.manifest(job_id)
    started = datetime.datetime.now(datetime.timezone.utc)

    publish(job_id, acknowledge_unverified_rights=True)

    manifest = project.manifest(job_id)
    record = manifest.pop(ACKNOWLEDGMENT_FIELD)
    assert record["acknowledged"] is True
    acknowledged_at = datetime.datetime.fromisoformat(record["acknowledged_at"])
    assert started <= acknowledged_at <= datetime.datetime.now(datetime.timezone.utc)
    # Nothing else in the manifest moved — least of all the rights_basis.
    assert manifest == before
    assert manifest["source"]["rights_basis"] == "unverified"


def test_the_acknowledgment_is_on_record_before_the_upload_begins(unverified, fake_youtube):
    _, job_id = unverified

    publish(job_id, acknowledge_unverified_rights=True)

    assert fake_youtube.acknowledgment_at_upload["acknowledged"] is True


def test_an_earlier_acknowledgment_does_not_cover_the_next_upload(unverified, fake_youtube):
    project, job_id = unverified
    publish(job_id, acknowledge_unverified_rights=True)
    assert ACKNOWLEDGMENT_FIELD in project.manifest(job_id)
    fake_youtube.uploads.clear()

    message = refused(project, job_id, fake_youtube)

    assert UNVERIFIED_ACK_LABEL in message


def test_nothing_is_uploaded_if_the_acknowledgment_cannot_be_recorded(
    unverified, fake_youtube, monkeypatch
):
    project, job_id = unverified
    before = project.manifest(job_id)

    def disk_full(src, dst):
        raise OSError("No space left on device")

    monkeypatch.setattr(youtube.os, "replace", disk_full)

    with pytest.raises(YouTubePublishError, match="nothing was uploaded"):
        publish(job_id, acknowledge_unverified_rights=True)

    assert fake_youtube.acknowledgment_at_upload == "upload never began"
    assert project.manifest(job_id) == before
    assert sorted(path.name for path in (project.staged / job_id).iterdir()) == [
        "manifest.json",
        "output.mp4",
    ]


def test_a_manifest_changed_during_sign_in_is_checked_again_before_the_upload(
    unverified, fake_youtube, monkeypatch
):
    project, job_id = unverified

    def sign_in_while_the_manifest_changes():
        edit_manifest(
            project, job_id, lambda manifest: manifest["format_compliance"].update(passed=False)
        )
        return fake_youtube

    monkeypatch.setattr(youtube, "_get_authenticated_service", sign_in_while_the_manifest_changes)

    with pytest.raises(YouTubePublishError, match="format-compliance"):
        publish(job_id, acknowledge_unverified_rights=True)

    assert fake_youtube.acknowledgment_at_upload == "upload never began"
    assert ACKNOWLEDGMENT_FIELD not in project.manifest(job_id)


def test_a_review_note_saved_during_sign_in_is_kept(unverified, fake_youtube, monkeypatch):
    project, job_id = unverified
    review = {"reviewed": True, "approved": True, "reviewed_at": "2026-11-15T12:00:00Z"}

    def sign_in_while_a_review_is_saved():
        edit_manifest(project, job_id, lambda manifest: manifest.update(review=review))
        return fake_youtube

    monkeypatch.setattr(youtube, "_get_authenticated_service", sign_in_while_a_review_is_saved)

    publish(job_id, acknowledge_unverified_rights=True)

    manifest = project.manifest(job_id)
    assert manifest["review"] == review
    assert manifest[ACKNOWLEDGMENT_FIELD]["acknowledged"] is True


def test_a_failed_sign_in_leaves_no_acknowledgment_on_record(unverified, monkeypatch):
    project, job_id = unverified

    def no_credentials():
        raise YouTubePublishError("no OAuth client secrets")

    monkeypatch.setattr(youtube, "_get_authenticated_service", no_credentials)

    with pytest.raises(YouTubePublishError, match="OAuth"):
        publish(job_id, acknowledge_unverified_rights=True)

    assert ACKNOWLEDGMENT_FIELD not in project.manifest(job_id)


# --- own and allowlist ------------------------------------------------------


def test_my_own_footage_is_published_without_any_acknowledgment(own, fake_youtube):
    project, job_id = own

    publish(job_id)

    assert len(fake_youtube.uploads) == 1
    assert ACKNOWLEDGMENT_FIELD not in project.manifest(job_id)


def test_an_acknowledgment_is_not_recorded_for_a_clip_that_did_not_need_one(own, fake_youtube):
    project, job_id = own

    publish(job_id, acknowledge_unverified_rights=True)

    assert ACKNOWLEDGMENT_FIELD not in project.manifest(job_id)


def test_an_allowlisted_clip_is_published_while_the_ledger_still_permits_it(
    allowlisted, fake_youtube, ledger
):
    project, job_id = allowlisted

    publish(job_id)

    assert len(fake_youtube.uploads) == 1
    assert ledger["asked"] == [("somepodcast", "https://www.youtube.com/@somepodcast")]
    assert ACKNOWLEDGMENT_FIELD not in project.manifest(job_id)


def test_an_allowlisted_clip_is_refused_once_the_ledger_stops_permitting_it(
    allowlisted, fake_youtube, ledger
):
    project, job_id = allowlisted
    ledger["permitted"] = False

    message = refused(project, job_id, fake_youtube)

    assert "no longer permits" in message


def test_acknowledging_does_not_rescue_a_lapsed_allowlist_clip(allowlisted, fake_youtube, ledger):
    project, job_id = allowlisted
    ledger["permitted"] = False

    refused(project, job_id, fake_youtube, acknowledge_unverified_rights=True)


def test_an_unreadable_ledger_stops_an_allowlisted_publish(allowlisted, fake_youtube, ledger):
    project, job_id = allowlisted
    ledger["broken"] = True

    message = refused(project, job_id, fake_youtube)

    assert "could not re-check the permissions ledger" in message


# --- provenance that is missing or has been changed -------------------------


def test_a_clip_with_no_provenance_block_is_refused(unverified, fake_youtube):
    project, job_id = unverified
    edit_manifest(project, job_id, lambda manifest: manifest.pop("source"))

    message = refused(project, job_id, fake_youtube, acknowledge_unverified_rights=True)

    assert "no recorded source provenance" in message


@pytest.mark.parametrize("field", ["rights_basis", "source_kind", "permission_ledger_reference"])
def test_a_clip_with_an_incomplete_provenance_block_is_refused(unverified, fake_youtube, field):
    project, job_id = unverified
    edit_manifest(project, job_id, lambda manifest: manifest["source"].pop(field))

    refused(project, job_id, fake_youtube, acknowledge_unverified_rights=True)


@pytest.mark.parametrize("rights_basis", ["verified", "", None, "OWN", True])
def test_a_clip_with_an_unknown_rights_basis_is_refused(unverified, fake_youtube, rights_basis):
    project, job_id = unverified

    def change(manifest):
        manifest["source"]["rights_basis"] = rights_basis

    edit_manifest(project, job_id, change)

    refused(project, job_id, fake_youtube, acknowledge_unverified_rights=True)


def test_relabelling_a_downloaded_clip_as_own_is_refused(unverified, fake_youtube):
    project, job_id = unverified

    def change(manifest):
        manifest["source"]["rights_basis"] = "own"

    edit_manifest(project, job_id, change)

    message = refused(project, job_id, fake_youtube)

    assert "only a local file can be marked own" in message


def test_a_manifest_that_no_longer_matches_its_source_is_refused(
    unverified, fake_youtube, ledger
):
    project, job_id = unverified

    # A block that is consistent in itself, but not what intake recorded.
    def change(manifest):
        manifest["source"] = dict(ALLOWLISTED)

    edit_manifest(project, job_id, change)

    message = refused(project, job_id, fake_youtube)

    assert "no longer matches the source" in message
    assert ledger["asked"] == []


def test_a_clip_whose_source_record_is_broken_is_refused(unverified, fake_youtube):
    project, job_id = unverified
    (project.source_dir / "metadata.json").write_text("{ not json", encoding="utf-8")

    refused(project, job_id, fake_youtube, acknowledge_unverified_rights=True)


def test_a_clip_can_still_be_published_after_its_source_is_cleared_out(unverified, fake_youtube):
    project, job_id = unverified
    shutil.rmtree(project.source_dir)

    refused(project, job_id, fake_youtube)
    publish(job_id, acknowledge_unverified_rights=True)

    assert len(fake_youtube.uploads) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda source: source.pop("license"),
        lambda source: source.update(rights_basis="own"),
        lambda source: source.update(rights_basis="allowlist"),
        lambda source: source.update(rights_basis="cleared"),
        lambda source: source.update(source_url=None),
    ],
    ids=["field-dropped", "relabelled-own", "allowlist-without-reference", "made-up-basis", "no-origin"],
)
def test_a_changed_manifest_is_still_caught_once_its_source_is_cleared_out(
    unverified, fake_youtube, ledger, change
):
    project, job_id = unverified
    shutil.rmtree(project.source_dir)
    edit_manifest(project, job_id, lambda manifest: change(manifest["source"]))

    refused(project, job_id, fake_youtube, acknowledge_unverified_rights=True)

    assert ledger["asked"] == []


def test_my_own_clip_cannot_be_published_once_its_source_record_is_gone(own, fake_youtube):
    project, job_id = own
    shutil.rmtree(project.source_dir)

    message = refused(project, job_id, fake_youtube, acknowledge_unverified_rights=True)

    assert "metadata.json" in message


def test_an_allowlisted_clip_cannot_be_published_once_its_source_record_is_gone(
    allowlisted, fake_youtube, ledger
):
    project, job_id = allowlisted
    shutil.rmtree(project.source_dir)

    refused(project, job_id, fake_youtube)

    assert ledger["asked"] == []


def test_pointing_a_relabelled_clip_at_a_source_that_does_not_exist_is_refused(
    unverified, fake_youtube, ledger
):
    project, job_id = unverified

    # A block that is consistent in itself, for a creator the ledger
    # permits, with the source_id moved so there is no record to compare.
    def change(manifest):
        manifest["source"] = dict(ALLOWLISTED)
        manifest["source_id"] = "20200101T000000Z-abcdef"

    edit_manifest(project, job_id, change)

    refused(project, job_id, fake_youtube)

    assert ledger["asked"] == []


@pytest.mark.parametrize(
    "strip",
    [
        lambda manifest: (manifest.pop("mode"), manifest["source"].pop("rights_basis")),
        lambda manifest: (manifest.pop("mode"), manifest.update(source="gone")),
        lambda manifest: (manifest.pop("mode"), manifest.pop("source")),
        lambda manifest: (manifest.pop("mode"), manifest.pop("source"), manifest.pop("source_id")),
    ],
    ids=["basis-dropped", "source-not-a-block", "source-dropped", "only-highlight-left"],
)
def test_a_clip_cannot_be_passed_off_as_a_commentary_job(unverified, fake_youtube, strip):
    project, job_id = unverified

    def change(manifest):
        strip(manifest)
        manifest["permission_ledger_reference"] = ALLOWLISTED["permission_ledger_reference"]

    edit_manifest(project, job_id, change)

    refused(project, job_id, fake_youtube, acknowledge_unverified_rights=True)


def test_the_source_video_is_not_needed_to_publish_a_clip(own, fake_youtube):
    project, job_id = own
    (project.source_dir / "source.mp4").unlink()

    publish(job_id)

    assert len(fake_youtube.uploads) == 1


def test_a_ledger_reference_added_at_the_top_does_not_skip_the_acknowledgment(
    unverified, fake_youtube
):
    project, job_id = unverified

    def change(manifest):
        manifest["permission_ledger_reference"] = ALLOWLISTED["permission_ledger_reference"]
        del manifest["mode"]

    edit_manifest(project, job_id, change)

    message = refused(project, job_id, fake_youtube)

    assert UNVERIFIED_ACK_LABEL in message


@pytest.mark.parametrize(
    "format_compliance",
    [None, {"passed": False, "reasons": ["too long"]}, "passed"],
    ids=["never-checked", "failed", "not-a-result"],
)
def test_a_clip_that_has_not_passed_the_format_check_is_refused(
    unverified, fake_youtube, format_compliance
):
    project, job_id = unverified

    def change(manifest):
        if format_compliance is None:
            del manifest["format_compliance"]
        else:
            manifest["format_compliance"] = format_compliance

    edit_manifest(project, job_id, change)

    message = refused(project, job_id, fake_youtube, acknowledge_unverified_rights=True)

    assert "format-compliance" in message


def test_a_manifest_that_is_not_a_job_is_refused_plainly(unverified, fake_youtube):
    project, job_id = unverified
    (project.staged / job_id / "manifest.json").write_text("[]", encoding="utf-8")

    with pytest.raises(YouTubePublishError, match="not a job manifest"):
        publish(job_id, acknowledge_unverified_rights=True)

    assert fake_youtube.uploads == []


# --- the two ways in: control panel and command line ------------------------


@pytest.fixture
def upload_calls(monkeypatch):
    calls = []

    def fake_upload(job_id, **kwargs):
        calls.append(kwargs)
        return {"video_id": "vid123", "url": "https://youtube.com/watch?v=vid123"}

    monkeypatch.setattr(api, "upload_video", fake_upload)
    monkeypatch.setattr(run_single, "upload_video", fake_upload)
    return calls


@pytest.mark.parametrize(
    "sent, passed_on",
    [
        ({}, False),
        ({"acknowledge_unverified_rights": True}, True),
        ({"acknowledge_unverified_rights": False}, False),
        ({"acknowledge_unverified_rights": "true"}, False),
        ({"acknowledge_unverified_rights": 1}, False),
    ],
)
def test_the_control_panel_passes_on_only_a_literal_true(upload_calls, capsys, sent, passed_on):
    code = api.main(["publish-youtube", json.dumps({"job_id": "job-1", "title": "T", **sent})])
    capsys.readouterr()

    assert code == 0
    assert upload_calls[0]["acknowledge_unverified_rights"] is passed_on


def test_the_control_panel_reports_a_refusal_as_an_error(unverified, capsys):
    _, job_id = unverified

    code = api.main(["publish-youtube", json.dumps({"job_id": job_id, "title": "T"})])
    answer = json.loads(capsys.readouterr().out)

    assert code == 1
    assert answer["ok"] is False
    assert UNVERIFIED_ACK_LABEL in answer["error"]


def test_the_publish_command_does_not_acknowledge_unless_told_to(upload_calls, capsys):
    run_single.main(["publish", "job-1", "A title"])
    run_single.main(["publish", "job-1", "A title", UNVERIFIED_ACK_FLAG])
    capsys.readouterr()

    assert [call["acknowledge_unverified_rights"] for call in upload_calls] == [False, True]


def test_cutting_points_at_the_publish_step_without_taking_it(
    dirs, tools, capsys, monkeypatch  # noqa: F811
):
    def must_not_upload(*args, **kwargs):
        raise AssertionError("cutting must never reach the upload")

    monkeypatch.setattr(youtube, "upload_video", must_not_upload)
    Project(dirs, UNVERIFIED)

    assert run_clip.main(["cut", SOURCE_ID, "--only", "1"]) == 0
    out = capsys.readouterr().out

    assert "Nothing is published" in out
    assert 'pipeline.orchestrate.run_single publish <job_id> "<title>"' in out
    assert UNVERIFIED_ACK_FLAG in out


def test_the_acknowledgment_flag_is_only_mentioned_for_an_unverified_source(
    dirs, tools, capsys  # noqa: F811
):
    Project(dirs, OWN)

    assert run_clip.main(["cut", SOURCE_ID, "--only", "1"]) == 0
    out = capsys.readouterr().out

    assert "run_single publish" in out
    assert UNVERIFIED_ACK_FLAG not in out


# --- the page, the docs and the code must say the same thing ----------------


def test_the_page_and_the_gate_agree_on_what_a_clip_job_is():
    page = APP_JS.read_text(encoding="utf-8")
    listed = re.search(r"const CLIP_JOB_KEYS = \[(.*?)\];", page).group(1)

    assert tuple(re.findall(r'"(\w+)"', listed)) == youtube.CLIP_JOB_KEYS


def test_every_key_that_marks_a_clip_job_is_one_cutting_writes(unverified):
    project, job_id = unverified

    assert set(youtube.CLIP_JOB_KEYS) <= set(project.manifest(job_id))


def test_the_checkbox_uses_the_words_the_refusal_names():
    page = APP_JS.read_text(encoding="utf-8")

    assert f"<span>{UNVERIFIED_ACK_LABEL}</span>" in page


def test_the_checkbox_is_never_ticked_for_the_person():
    page = APP_JS.read_text(encoding="utf-8")
    boxes = re.findall(r"<input[^>]*yt-ack-rights[^>]*>", page)

    assert len(boxes) == 1
    assert "checked" not in boxes[0]
    assert ".checked = true" not in page


def test_the_page_sends_the_field_the_control_api_reads():
    page = APP_JS.read_text(encoding="utf-8")
    control_api = Path(api.__file__).read_text(encoding="utf-8")

    assert "args.acknowledge_unverified_rights = true" in page
    assert 'args.get("acknowledge_unverified_rights") is True' in control_api


@pytest.mark.parametrize("doc", ["README.md", "docs/pipeline-stages.md"])
def test_the_docs_describe_the_gate_in_the_same_words(doc):
    text = " ".join((REPO / doc).read_text(encoding="utf-8").split())

    assert UNVERIFIED_ACK_LABEL in text
    assert UNVERIFIED_ACK_FLAG in text
    for rights_basis in ("allowlist", "own", "unverified"):
        assert f"`{rights_basis}`" in text
