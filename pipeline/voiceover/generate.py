"""AI voiceover generation for a human-approved commentary script.

Takes the human-approved work/<job_id>/script.md (Step 5's output,
loaded via pipeline.script.draft.load_script) and synthesizes it into a
voiceover audio track via a configured TTS provider, writing the result
to work/<job_id>/voiceover.<ext>. This is AI-generated voiceover only —
no recorded-human-voice path exists here (explicitly deferred, see
PLAN.md Step 6).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pipeline.script.draft import (
    _SAFE_PATH_COMPONENT,
    extract_spoken_text,
    load_script,
    record_used_fact,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORK_DIR = REPO_ROOT / "work"
ENV_PATH = REPO_ROOT / ".env"

_ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_AUDIO_EXT = "mp3"


class VoiceoverError(ValueError):
    """Raised when TTS configuration is missing/invalid or synthesis fails."""


def _load_env_file() -> None:
    """Load KEY=VALUE pairs from a repo-root .env file into os.environ,
    without overriding variables already set in the real environment."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise VoiceoverError(
            f"{name} is not set. Configure it in .env before generating a voiceover."
        )
    return value


def _validate_job_id(job_id: str) -> None:
    if not _SAFE_PATH_COMPONENT.match(job_id):
        raise VoiceoverError(
            f"job_id '{job_id}' must contain only letters, digits, '.', '_', or '-'"
        )


def synthesize_speech(text: str) -> bytes:
    """Call the configured TTS provider and return raw audio bytes. Fails
    loudly on missing config, an unsupported provider, or a non-2xx
    response — never falls back to silence or a placeholder track."""
    _load_env_file()
    provider = os.environ.get("TTS_PROVIDER", "elevenlabs").strip().lower()
    if provider != "elevenlabs":
        raise VoiceoverError(
            f"unsupported TTS_PROVIDER '{provider}' — only 'elevenlabs' is implemented"
        )

    api_key = _require_env("ELEVENLABS_API_KEY")
    voice_id = _require_env("ELEVENLABS_VOICE_ID")
    model_id = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()

    request = urllib.request.Request(
        _ELEVENLABS_TTS_URL.format(voice_id=urllib.parse.quote(voice_id, safe="")),
        data=json.dumps({"text": text, "model_id": model_id}).encode("utf-8"),
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise VoiceoverError(f"TTS request failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise VoiceoverError(f"TTS request failed: {exc.reason}") from exc


def generate_voiceover(job_id: str) -> Path:
    """Synthesize the human-approved script for job_id into voiceover
    audio, writing it to work/<job_id>/voiceover.<ext> and returning that
    path. Raises VoiceoverError if a voiceover already exists for this
    job, or if TTS configuration or the provider call fails; raises
    DraftError (from load_script) if no approved script exists yet."""
    _validate_job_id(job_id)
    output_path = WORK_DIR / job_id / f"voiceover.{_AUDIO_EXT}"
    if output_path.exists():
        raise VoiceoverError(f"voiceover already exists at {output_path}")

    script_text = load_script(job_id)
    spoken_text = extract_spoken_text(script_text)

    audio_bytes = synthesize_speech(spoken_text)
    if not audio_bytes:
        raise VoiceoverError(f"TTS provider returned empty audio for job '{job_id}'")

    output_path.write_bytes(audio_bytes)

    # Voiceover generation is the point Step 5's draft is treated as final
    # (see load_script's docstring), so this is where the fact/hook it's
    # built around gets recorded to avoid a future script repeating it.
    hook = spoken_text.split("\n\n", 1)[0].strip()
    record_used_fact(job_id, hook)

    return output_path
