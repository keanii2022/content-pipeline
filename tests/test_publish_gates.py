"""The YouTube publish step must refuse jobs that aren't cleared to ship.

upload_video() checks a staged job's manifest before it ever logs in to
YouTube. These tests build fake staged jobs in a temp folder and swap the
YouTube login for a stand-in, so nothing real is uploaded.
"""

import json

import pytest

from pipeline.publish import youtube


class ReachedLogin(Exception):
    """Raised by the fake login — means every safety check was passed."""


@pytest.fixture
def staged_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "STAGED_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def login_attempts(monkeypatch):
    attempts = []

    def fake_login():
        attempts.append(1)
        raise ReachedLogin()

    monkeypatch.setattr(youtube, "_get_authenticated_service", fake_login)
    return attempts


def stage_job(staged_dir, job_id, manifest):
    job_dir = staged_dir / job_id
    job_dir.mkdir()
    (job_dir / "output.mp4").write_bytes(b"")
    (job_dir / "manifest.json").write_text(json.dumps(manifest))


def eligible_manifest():
    return {
        "job_id": "job-1",
        "output_file": "output.mp4",
        "permission_ledger_reference": {
            "creator_id": "nasa",
            "source_url": "https://www.nasa.gov",
            "permission_granted_date": "2026-09-01",
        },
        "format_compliance": {"passed": True, "reasons": []},
    }


def test_publish_refuses_job_missing_permission_provenance(staged_dir, login_attempts):
    manifest = eligible_manifest()
    del manifest["permission_ledger_reference"]
    stage_job(staged_dir, "job-1", manifest)

    with pytest.raises(youtube.YouTubePublishError, match="permission_ledger_reference"):
        youtube.upload_video("job-1", title="A real title")

    assert login_attempts == []


@pytest.mark.parametrize(
    "format_compliance",
    [None, {"passed": False, "reasons": ["too long"]}],
    ids=["format-check-never-ran", "format-check-failed"],
)
def test_publish_refuses_job_without_passed_format_check(
    staged_dir, login_attempts, format_compliance
):
    manifest = eligible_manifest()
    if format_compliance is None:
        del manifest["format_compliance"]
    else:
        manifest["format_compliance"] = format_compliance
    stage_job(staged_dir, "job-1", manifest)

    with pytest.raises(youtube.YouTubePublishError, match="format-compliance"):
        youtube.upload_video("job-1", title="A real title")

    assert login_attempts == []


def test_publish_lets_an_eligible_job_through_to_login(staged_dir, login_attempts):
    stage_job(staged_dir, "job-1", eligible_manifest())

    with pytest.raises(ReachedLogin):
        youtube.upload_video("job-1", title="A real title")

    assert login_attempts == [1]
