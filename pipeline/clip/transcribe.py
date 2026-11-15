"""Clip-mode word-level transcription.

Runs Whisper locally (faster-whisper, on the CPU) over a source taken in by
pipeline.clip.intake and writes two files to work/clip-<source_id>/:

  transcript.json  language, duration, and every segment with per-word
                   timings — what the cutter and later captions work from
  transcript.txt   one "[mm:ss] text" line per segment — what the
                   highlight-finder agent reads

The model size comes from WHISPER_MODEL (in .env or the environment),
default "small". The source's provenance is copied into transcript.json
unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from pipeline.clip.intake import IntakeError, load_source, provenance
from pipeline.voiceover.generate import _load_env_file

REPO_ROOT = Path(__file__).resolve().parents[2]
WORK_DIR = REPO_ROOT / "work"

DEFAULT_MODEL_SIZE = "small"
# A fixed list, not whatever the setting says: faster-whisper would treat
# any other string as a Hugging Face repo or local path to load a model from.
MODEL_SIZES = (
    "tiny", "tiny.en",
    "base", "base.en",
    "small", "small.en",
    "medium", "medium.en",
    "large-v2", "large-v3", "large-v3-turbo",
)


class TranscribeError(ValueError):
    """Raised when a source can't be transcribed."""


def _resolve_model_size(model_size: Optional[str]) -> str:
    if model_size is None:
        _load_env_file()
        model_size = os.environ.get("WHISPER_MODEL", "").strip() or DEFAULT_MODEL_SIZE
    if model_size not in MODEL_SIZES:
        raise TranscribeError(
            f"Whisper model '{model_size}' is not one of {', '.join(MODEL_SIZES)}"
        )
    return model_size


def _load_model(model_size: str) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscribeError(
            'faster-whisper is not installed — run: .venv/bin/pip install -e ".[clip]"'
        ) from exc
    try:
        return WhisperModel(model_size, device="cpu", compute_type="int8")
    except Exception as exc:
        raise TranscribeError(f"could not load Whisper model '{model_size}': {exc}") from exc


def _decode_audio(source_path: Path, ffmpeg_path: str) -> Any:
    """Return the source's first audio track as the 16 kHz mono float array
    Whisper takes.

    Decoded here with ffmpeg rather than by handing faster-whisper the file:
    its own loader goes through PyAV, and faster-whisper 1.2.1 fails on
    PyAV 19 ("unexpected keyword argument 'metadata_errors'").
    """
    import numpy  # installed with faster-whisper; not a base dependency

    cmd = [
        ffmpeg_path,
        "-nostdin",
        "-v", "error",
        "-i", str(source_path),
        "-map", "0:a:0",
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-f", "f32le",
        "-",
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise TranscribeError(f"'{ffmpeg_path}' not found — is ffmpeg installed?") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise TranscribeError(f"ffmpeg could not read the audio (exit {exc.returncode}): {detail}") from exc
    if not result.stdout:
        raise TranscribeError(f"ffmpeg found no audio in {source_path.name}")
    return numpy.frombuffer(result.stdout, dtype=numpy.float32)


def _seconds(value: Any) -> float:
    return round(float(value), 3)


def _segment_record(segment: Any) -> Optional[dict[str, Any]]:
    text = (segment.text or "").strip()
    if not text:
        return None
    # A word Whisper couldn't time is left out rather than failing the run;
    # the segment's own text and timing still cover it.
    words = [
        {"start": _seconds(word.start), "end": _seconds(word.end), "word": word.word.strip()}
        for word in (segment.words or [])
        if word.start is not None and word.end is not None and (word.word or "").strip()
    ]
    return {
        "start": _seconds(segment.start),
        "end": _seconds(segment.end),
        "text": text,
        "words": words,
    }


def _timestamp(seconds: float) -> str:
    # Minutes keep counting past 59 ("[125:07]") so every marker has one shape.
    whole = int(seconds)
    return f"[{whole // 60:02d}:{whole % 60:02d}]"


def _write_together(files: dict[Path, str]) -> None:
    """Write several files so a failure can't leave them out of step.

    Everything is written beside its target first and only then renamed
    into place: a full disk or a crash mid-write keeps the previous
    transcript.json and transcript.txt as a matching pair, instead of
    pairing new timings with old text.
    """
    partials = {path: path.with_name(path.name + ".partial") for path in files}
    try:
        for path, text in files.items():
            partials[path].write_text(text, encoding="utf-8")
        for path, partial in partials.items():
            partial.replace(path)
    finally:
        for partial in partials.values():
            partial.unlink(missing_ok=True)


def transcribe(
    source_id: str,
    *,
    model_size: Optional[str] = None,
    model: Any = None,
    on_progress: Optional[Callable[[float], None]] = None,
    ffmpeg_path: str = "ffmpeg",
) -> Path:
    """Transcribe one clip-mode source; return its work/clip-<source_id>/ folder.

    `model` lets a caller supply an already-loaded (or stand-in) model.
    Running it again replaces the previous transcript.
    """
    try:
        source_path, metadata = load_source(source_id)
    except IntakeError as exc:
        raise TranscribeError(str(exc)) from exc

    model_size = _resolve_model_size(model_size)
    if model is None:
        model = _load_model(model_size)

    audio = _decode_audio(source_path, ffmpeg_path)
    try:
        segments, info = model.transcribe(
            audio,
            word_timestamps=True,
            # Skips silence/music, where Whisper otherwise invents text.
            vad_filter=True,
        )
        duration = float(info.duration)
        records = []
        # `segments` is lazy: the actual transcription happens in this loop.
        for segment in segments:
            record = _segment_record(segment)
            if record:
                records.append(record)
            if on_progress and duration > 0:
                on_progress(min(float(segment.end) / duration, 1.0))
    except TranscribeError:
        raise
    except Exception as exc:
        raise TranscribeError(f"Whisper failed on {source_path.name}: {exc}") from exc

    if not records:
        raise TranscribeError(f"no speech found in {source_path.name}")

    work_dir = WORK_DIR / f"clip-{source_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    transcript = {
        "source_id": source_id,
        "language": info.language,
        "duration_seconds": _seconds(duration),
        "whisper_model": model_size,
        "source": provenance(metadata),
        "segments": records,
    }
    _write_together({
        work_dir / "transcript.json": json.dumps(transcript, indent=2, ensure_ascii=False) + "\n",
        work_dir / "transcript.txt": "".join(
            f"{_timestamp(record['start'])} {record['text']}\n" for record in records
        ),
    })
    return work_dir


def load_transcript(source_id: str) -> tuple[Path, dict[str, Any]]:
    """Return (work/clip-<source_id>/, its transcript) for a later step.

    Refuses a transcript whose provenance block no longer matches the
    source's metadata.json, so a later step can't build on a transcript
    that says something different about where the video came from.
    """
    try:
        _, metadata = load_source(source_id)
    except IntakeError as exc:
        raise TranscribeError(str(exc)) from exc

    work_dir = WORK_DIR / f"clip-{source_id}"
    transcript_path = work_dir / "transcript.json"
    if not transcript_path.is_file():
        raise TranscribeError(f"no transcript for '{source_id}' — run transcribe first")
    try:
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TranscribeError(f"{transcript_path} is not valid JSON") from exc
    if not isinstance(transcript, dict) or transcript.get("source_id") != source_id:
        raise TranscribeError(f"{transcript_path} does not describe source '{source_id}'")
    if transcript.get("source") != provenance(metadata):
        raise TranscribeError(
            f"{transcript_path} records different provenance from the source's "
            "metadata.json — transcribe it again"
        )

    duration = transcript.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        raise TranscribeError(f"{transcript_path} has no usable 'duration_seconds'")
    if not isinstance(transcript.get("segments"), list) or not transcript["segments"]:
        raise TranscribeError(f"{transcript_path} has no segments")
    return work_dir, transcript


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.clip.transcribe",
        description="Transcribe a clip-mode source (word-level) with local Whisper.",
    )
    parser.add_argument("source_id", help="the id printed by pipeline.clip.intake")
    parser.add_argument(
        "--model",
        choices=MODEL_SIZES,
        help=f"Whisper model size (default: WHISPER_MODEL in .env, else {DEFAULT_MODEL_SIZE})",
    )
    args = parser.parse_args(argv)

    def show_progress(fraction: float) -> None:
        print(f"\rtranscribing… {fraction:4.0%}", end="", file=sys.stderr, flush=True)

    try:
        work_dir = transcribe(args.source_id, model_size=args.model, on_progress=show_progress)
    except TranscribeError as exc:
        print(f"\ntranscribe failed: {exc}", file=sys.stderr)
        return 1
    print(file=sys.stderr)

    transcript = json.loads((work_dir / "transcript.json").read_text(encoding="utf-8"))
    print(f"language:   {transcript['language']}")
    print(f"segments:   {len(transcript['segments'])}")
    print(f"transcript: {work_dir / 'transcript.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
